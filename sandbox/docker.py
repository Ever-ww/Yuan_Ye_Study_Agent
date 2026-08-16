"""Trace 级 Docker 容器与危险 Bash 执行控制器。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from backup.security import SensitiveEnvSanitizer

from .checkpoint import CheckpointStore
from .locks import WorkspaceLockManager
from .models import (
    BashResult, CheckpointBranchRecord, CheckpointMergeAttempt, CheckpointRecord,
    RollbackResult, SandboxStatus,
)


class DockerUnavailableError(RuntimeError):
    """仅表示 Docker CLI 缺失或 daemon 当前无法连接。"""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class BashUnavailableError(RuntimeError):
    """当前 Trace 没有可安全执行 Bash 的 Docker 容器。"""


class CommandResult(BaseModel):
    """外部命令执行结果，便于测试注入而不依赖真实 Docker。"""

    model_config = ConfigDict(frozen=True, strict=True)

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str], float | None], Awaitable[CommandResult]]


class SandboxSessionProtocol(Protocol):
    """工具与 Runtime 之间共享的最小沙箱契约。"""

    file_locks: WorkspaceLockManager

    @property
    def status(self) -> SandboxStatus: ...
    @property
    def bash_available(self) -> bool: ...

    async def start(self, session_id: str) -> CheckpointRecord: ...
    async def close(self) -> None: ...
    async def run_bash(self, command: str, timeout_seconds: int = 30) -> BashResult: ...
    async def checkpoint_write(self, path: str) -> CheckpointRecord | None: ...
    async def checkpoint_edit(self, path: str) -> CheckpointRecord | None: ...
    async def restore_current(self) -> CheckpointRecord: ...
    async def rollback(
        self, steps: int | None = None, *, sequence: int | None = None,
        checkpoint_sha: str | None = None, merge_eligible: bool = True,
        archive_reason: str = "user_rollback",
    ) -> RollbackResult: ...
    def list_checkpoints(self) -> tuple[CheckpointRecord, ...]: ...
    def list_checkpoint_branches(self) -> tuple[CheckpointBranchRecord, ...]: ...
    def list_checkpoint_merge_attempts(self) -> tuple[CheckpointMergeAttempt, ...]: ...
    async def set_checkpoint_branch_merge_eligibility(
        self, branch_id: str, eligible: bool, reason: str,
    ) -> CheckpointBranchRecord: ...


class DockerSandboxSession:
    """把 Bash 限制在 Docker 中，同时让项目文件与宿主机实时联动。"""

    image = "yy-agent-sandbox:local"

    def __init__(
        self,
        project_root: Path,
        *,
        state_root: Path | None = None,
        checkpoint_limit: int = 17,
        command_runner: CommandRunner | None = None,
        checkpoint_store: CheckpointStore | None = None,
        file_locks: WorkspaceLockManager | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.state_root = (state_root or project_root).resolve()
        self.file_locks = file_locks or WorkspaceLockManager(
            self.project_root,
            state_root=self.state_root,
        )
        self.checkpoints = checkpoint_store or CheckpointStore(
            self.project_root,
            state_root=self.state_root,
            limit=checkpoint_limit,
        )
        self._run_command = command_runner or _subprocess_runner
        self._container_name: str | None = None
        self._operation_lock = asyncio.Lock()
        self._status = SandboxStatus(
            mode="pending",
            bash_available=False,
            message="沙箱尚未探测",
        )

    @property
    def active(self) -> bool:
        return self._status.mode in {"docker", "checkpoint_only"}

    @property
    def status(self) -> SandboxStatus:
        return self._status

    @property
    def bash_available(self) -> bool:
        return self._status.bash_available

    async def start(self, session_id: str) -> CheckpointRecord:
        """验证 Docker、启动受限容器并创建 Trace 基线快照。"""
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                if self.active:
                    records = self.checkpoints.list()
                    if not records:
                        raise RuntimeError("沙箱已启动但缺少基线 checkpoint")
                    return records[-1]
                self._status = SandboxStatus(
                    mode="pending",
                    bash_available=False,
                    message="正在探测 Docker",
                )
                try:
                    await self._require_docker()
                except DockerUnavailableError as exc:
                    baseline = await self._open_checkpoint_baseline(session_id)
                    self._status = SandboxStatus(
                        mode="checkpoint_only",
                        bash_available=False,
                        reason_code=exc.reason_code,
                        message=(
                            f"{exc}；已进入 checkpoint-only 模式，Bash 已禁用，"
                            "本地文件写入与回溯仍可用"
                        ),
                    )
                    return baseline
                await self._ensure_image()
                container_name = f"yy-agent-{_container_fragment(session_id)}-{uuid4().hex[:8]}"
                arguments = self._docker_run_arguments(container_name)
                result = await self._run_command(arguments, 30)
                if result.returncode != 0:
                    raise RuntimeError(f"Docker 沙箱启动失败：{_result_message(result)}")
                self._container_name = container_name
                try:
                    baseline = await self._open_checkpoint_baseline(session_id)
                    self._status = SandboxStatus(
                        mode="docker",
                        bash_available=True,
                        message="Docker 沙箱已启动，Bash 可用",
                    )
                    return baseline
                except Exception:
                    await self._close_unlocked()
                    raise

    async def close(self) -> None:
        """直接销毁当前 Trace 的 Docker 容器，保留本地 checkpoint。"""
        async with self._operation_lock:
            await self._close_unlocked()

    async def run_bash(self, command: str, timeout_seconds: int = 30) -> BashResult:
        """在 Docker 中运行 Bash；失败时恢复到调用前 checkpoint。"""
        if not command.strip():
            raise ValueError("Bash command 不能为空")
        if timeout_seconds < 1 or timeout_seconds > 120:
            raise ValueError("Bash timeout_seconds 必须位于 1 到 120 之间")
        if not self.bash_available:
            raise BashUnavailableError(
                "当前 Trace 处于 checkpoint-only 模式，Bash 不可用且不会回退到宿主机 Shell",
            )
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                container = self._require_container()
                arguments = [
                    "docker",
                    "exec",
                    "--workdir",
                    "/workspace",
                    container,
                    "timeout",
                    "--signal=KILL",
                    f"{timeout_seconds}s",
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    command,
                ]
                try:
                    result = await self._run_command(arguments, timeout_seconds + 5)
                except Exception:
                    await asyncio.to_thread(self.checkpoints.restore_current)
                    raise
                output = _bounded_output(result.stdout, result.stderr)
                if result.returncode != 0:
                    await asyncio.to_thread(self.checkpoints.restore_current)
                    raise RuntimeError(
                        f"Docker Bash 执行失败（exit={result.returncode}）：{output or '无输出'}",
                    )
                try:
                    checkpoint = await asyncio.to_thread(
                        self.checkpoints.create,
                        "bash",
                        {"command": command[:1000], "timeout_seconds": timeout_seconds},
                    )
                except Exception:
                    await asyncio.to_thread(self.checkpoints.restore_current)
                    raise
                return BashResult(exit_code=0, output=output, checkpoint=checkpoint)

    async def checkpoint_write(self, path: str) -> CheckpointRecord | None:
        """为宿主机 write 已完成的实际修改创建一次 checkpoint。"""
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(
                self.checkpoints.create,
                "write",
                {"path": path},
            )

    async def checkpoint_edit(self, path: str) -> CheckpointRecord | None:
        """为宿主机 edit 已完成的实际修改创建一次独立审计 checkpoint。"""
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(
                self.checkpoints.create,
                "edit",
                {"path": path},
            )

    async def restore_current(self) -> CheckpointRecord:
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(self.checkpoints.restore_current)

    async def rollback(
        self,
        steps: int | None = None,
        *,
        sequence: int | None = None,
        checkpoint_sha: str | None = None,
        merge_eligible: bool = True,
        archive_reason: str = "user_rollback",
    ) -> RollbackResult:
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                self._require_checkpoint_session()
                if (
                    steps is not None and sequence is None and checkpoint_sha is None
                    and merge_eligible and archive_reason == "user_rollback"
                ):
                    # 保持旧测试替身和第三方Sandbox适配器的单参数调用兼容性。
                    return await asyncio.to_thread(self.checkpoints.rollback, steps)
                return await asyncio.to_thread(
                    self.checkpoints.rollback,
                    steps,
                    sequence=sequence,
                    checkpoint_sha=checkpoint_sha,
                    merge_eligible=merge_eligible,
                    archive_reason=archive_reason,
                )

    def list_checkpoints(self) -> tuple[CheckpointRecord, ...]:
        return self.checkpoints.list()

    def list_checkpoint_branches(self) -> tuple[CheckpointBranchRecord, ...]:
        return self.checkpoints.list_branches()

    def list_checkpoint_merge_attempts(self) -> tuple[CheckpointMergeAttempt, ...]:
        """返回只读的 Dream 合并历史，供状态查询和人工恢复判断使用。"""
        return self.checkpoints.list_merge_attempts()

    async def set_checkpoint_branch_merge_eligibility(
        self,
        branch_id: str,
        eligible: bool,
        reason: str,
    ) -> CheckpointBranchRecord:
        """串行修改归档分支的 Dream 准入；不触碰工作区或项目 Git。"""
        async with self._operation_lock:
            self._require_checkpoint_session()
            return await asyncio.to_thread(
                self.checkpoints.set_merge_eligibility,
                branch_id,
                eligible,
                reason,
            )

    async def _require_docker(self) -> None:
        if shutil.which("docker") is None and self._run_command is _subprocess_runner:
            raise DockerUnavailableError(
                "未找到 Docker CLI",
                reason_code="docker_cli_missing",
            )
        try:
            result = await self._run_command(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                15,
            )
        except (FileNotFoundError, OSError) as exc:
            raise DockerUnavailableError(
                "无法执行 Docker CLI",
                reason_code="docker_cli_missing",
            ) from exc
        except RuntimeError as exc:
            raise DockerUnavailableError(
                f"Docker daemon 无法连接：{exc}",
                reason_code="docker_daemon_unavailable",
            ) from exc
        if result.returncode != 0:
            raise DockerUnavailableError(
                f"Docker daemon 无法连接：{_result_message(result)}",
                reason_code="docker_daemon_unavailable",
            )

    async def _open_checkpoint_baseline(self, session_id: str) -> CheckpointRecord:
        """初始化独立 Git 对象库；失败属于数据安全问题，必须向上抛出。"""
        self.checkpoints.open(session_id)
        baseline = await asyncio.to_thread(
            self.checkpoints.create,
            "trace_start",
            {"kind": "baseline"},
            force=True,
        )
        if baseline is None:
            raise RuntimeError("创建 Trace 基线 checkpoint 失败")
        return baseline

    async def _ensure_image(self) -> None:
        inspected = await self._run_command(["docker", "image", "inspect", self.image], 15)
        if inspected.returncode == 0:
            return
        dockerfile = Path(__file__).with_name("Dockerfile")
        built = await self._run_command(
            [
                "docker",
                "build",
                "--tag",
                self.image,
                "--file",
                str(dockerfile),
                str(dockerfile.parent),
            ],
            300,
        )
        if built.returncode != 0:
            raise RuntimeError(f"Docker 沙箱镜像构建失败：{_result_message(built)}")

    def _docker_run_arguments(self, container_name: str) -> list[str]:
        mount = f"type=bind,source={self.project_root},target=/workspace"
        arguments = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--pids-limit",
            "256",
            "--read-only",
            "--mount",
            mount,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=128m",
        ]
        for relative in (".git", ".yy", ".venv", ".agents", ".codex"):
            arguments.extend([
                "--tmpfs",
                f"/workspace/{relative}:rw,noexec,nosuid,nodev,size=16m",
            ])
        blank = self.state_root / ".yy" / "sandbox" / "empty-secret"
        blank.parent.mkdir(parents=True, exist_ok=True)
        blank.touch(exist_ok=True)
        for path in _environment_files(self.project_root):
            relative = PurePosixPath(path.relative_to(self.project_root).as_posix())
            arguments.extend([
                "--mount",
                f"type=bind,source={blank},target=/workspace/{relative},readonly",
            ])
        arguments.append(self.image)
        return arguments

    async def _close_unlocked(self) -> None:
        container = self._container_name
        self._container_name = None
        self._status = SandboxStatus(
            mode="closed",
            bash_available=False,
            message="沙箱会话已关闭，checkpoint 已保留",
        )
        if container is None:
            return
        result = await self._run_command(["docker", "rm", "--force", container], 30)
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise RuntimeError(f"Docker 沙箱关闭失败：{_result_message(result)}")

    def _require_container(self) -> str:
        if self._container_name is None:
            raise BashUnavailableError(
                "Docker 沙箱尚未启动或已经关闭；Bash 不会回退到宿主机 Shell",
            )
        return self._container_name

    def _require_checkpoint_session(self) -> None:
        if not self.active:
            raise RuntimeError("Checkpoint 会话尚未启动或已经关闭")


async def probe_docker_status(
    command_runner: CommandRunner | None = None,
) -> SandboxStatus:
    """只探测 CLI/daemon，不构建镜像或创建容器。"""
    runner = command_runner or _subprocess_runner
    if command_runner is None and shutil.which("docker") is None:
        return SandboxStatus(
            mode="checkpoint_only",
            bash_available=False,
            reason_code="docker_cli_missing",
            message="未找到 Docker CLI；将使用 checkpoint-only 模式",
        )
    try:
        result = await runner(["docker", "version", "--format", "{{.Server.Version}}"], 15)
    except (FileNotFoundError, OSError):
        return SandboxStatus(
            mode="checkpoint_only",
            bash_available=False,
            reason_code="docker_cli_missing",
            message="无法执行 Docker CLI；将使用 checkpoint-only 模式",
        )
    except Exception as exc:
        return SandboxStatus(
            mode="checkpoint_only",
            bash_available=False,
            reason_code="docker_daemon_unavailable",
            message=f"Docker daemon 无法连接：{exc}",
        )
    if result.returncode != 0:
        return SandboxStatus(
            mode="checkpoint_only",
            bash_available=False,
            reason_code="docker_daemon_unavailable",
            message=f"Docker daemon 无法连接：{_result_message(result)}",
        )
    return SandboxStatus(
        mode="docker",
        bash_available=True,
        message="Docker daemon 可用",
    )


def sandbox_status_of(sandbox: object | None) -> SandboxStatus:
    """读取正式状态；旧注入对象缺少状态时按最小权限处理。"""
    if sandbox is None:
        return SandboxStatus(
            mode="closed",
            bash_available=False,
            reason_code="sandbox_disabled",
            message="当前 Runtime 未启用沙箱与 checkpoint",
        )
    value = getattr(sandbox, "status", None)
    if isinstance(value, SandboxStatus):
        return value
    if isinstance(value, dict):
        return SandboxStatus.model_validate(value)
    return SandboxStatus(
        mode="checkpoint_only",
        bash_available=False,
        reason_code="injected_checkpoint_only",
        message="注入的执行器未声明 Docker 状态，按 checkpoint-only 处理，Bash 已禁用",
    )


async def _subprocess_runner(arguments: list[str], timeout: float | None) -> CommandResult:
    """用参数数组执行外部命令，禁止宿主机 Shell 解释模型输入。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=SensitiveEnvSanitizer.subprocess_env(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"外部命令执行超时：{arguments[0]}") from exc
    return CommandResult(
        returncode=int(process.returncode or 0),
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _environment_files(root: Path) -> list[Path]:
    """查找需要遮蔽的环境文件，同时避免遍历 Git、虚拟环境和本机状态。"""
    excluded = {".git", ".yy", ".venv", ".agents", ".codex"}
    values: list[Path] = []
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in excluded]
        base = Path(directory)
        for name in files:
            if name == ".env" or name.startswith(".env."):
                values.append(base / name)
    return values


def _container_fragment(session_id: str) -> str:
    value = "".join(character.lower() if character.isalnum() else "-" for character in session_id)
    return value.strip("-")[:24] or "session"


def _bounded_output(stdout: str, stderr: str, limit: int = 20000) -> str:
    value = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    return value if len(value) <= limit else value[:limit] + "\n…（输出已截断）"


def _result_message(result: CommandResult) -> str:
    return (result.stderr or result.stdout or f"exit={result.returncode}").strip()
