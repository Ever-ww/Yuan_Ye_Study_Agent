"""Trace 级 Docker 容器与危险 Bash 执行控制器。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .checkpoint import CheckpointStore
from .locks import WorkspaceLockManager
from .models import BashResult, CheckpointRecord, RollbackResult


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

    async def start(self, session_id: str) -> CheckpointRecord: ...
    async def close(self) -> None: ...
    async def run_bash(self, command: str, timeout_seconds: int = 30) -> BashResult: ...
    async def checkpoint_write(self, path: str) -> CheckpointRecord | None: ...
    async def restore_current(self) -> CheckpointRecord: ...
    async def rollback(self, steps: int) -> RollbackResult: ...
    def list_checkpoints(self) -> tuple[CheckpointRecord, ...]: ...


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

    @property
    def active(self) -> bool:
        return self._container_name is not None

    async def start(self, session_id: str) -> CheckpointRecord:
        """验证 Docker、启动受限容器并创建 Trace 基线快照。"""
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                if self._container_name is not None:
                    records = self.checkpoints.list()
                    if not records:
                        raise RuntimeError("沙箱已启动但缺少基线 checkpoint")
                    return records[-1]
                await self._require_docker()
                await self._ensure_image()
                container_name = f"yy-agent-{_container_fragment(session_id)}-{uuid4().hex[:8]}"
                arguments = self._docker_run_arguments(container_name)
                result = await self._run_command(arguments, 30)
                if result.returncode != 0:
                    raise RuntimeError(f"Docker 沙箱启动失败：{_result_message(result)}")
                self._container_name = container_name
                try:
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
        """为宿主机 write_file 已完成的实际修改创建一次 checkpoint。"""
        async with self._operation_lock:
            self._require_container()
            return await asyncio.to_thread(
                self.checkpoints.create,
                "write_file",
                {"path": path},
            )

    async def restore_current(self) -> CheckpointRecord:
        async with self._operation_lock:
            return await asyncio.to_thread(self.checkpoints.restore_current)

    async def rollback(self, steps: int) -> RollbackResult:
        async with self.file_locks.workspace_exclusive():
            async with self._operation_lock:
                self._require_container()
                return await asyncio.to_thread(self.checkpoints.rollback, steps)

    def list_checkpoints(self) -> tuple[CheckpointRecord, ...]:
        return self.checkpoints.list()

    async def _require_docker(self) -> None:
        if shutil.which("docker") is None and self._run_command is _subprocess_runner:
            raise RuntimeError("未找到 Docker CLI；请安装并启动 Docker Desktop")
        result = await self._run_command(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker 服务不可用：{_result_message(result)}")

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
        if container is None:
            return
        result = await self._run_command(["docker", "rm", "--force", container], 30)
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise RuntimeError(f"Docker 沙箱关闭失败：{_result_message(result)}")

    def _require_container(self) -> str:
        if self._container_name is None:
            raise RuntimeError("Docker 沙箱尚未启动或已经关闭")
        return self._container_name


async def _subprocess_runner(arguments: list[str], timeout: float | None) -> CommandResult:
    """用参数数组执行外部命令，禁止宿主机 Shell 解释模型输入。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
