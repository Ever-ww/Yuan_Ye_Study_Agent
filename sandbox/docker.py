"""Trace 级 Docker 容器与危险 Bash 执行控制器。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path, PurePosixPath
from uuid import uuid4

from backup.security import SensitiveEnvSanitizer

from .models import SandboxStatus


from .session import (
    BashUnavailableError, CommandResult, CommandRunner, SandboxSessionProtocol,
    SandboxUnavailableError, CheckpointSandboxSession,
)

class DockerUnavailableError(SandboxUnavailableError):
    """Docker CLI or daemon is unavailable."""

class DockerSandboxSession(CheckpointSandboxSession):
    """Explicit alternative backend; lifecycle and checkpoint logic are shared."""
    image = "yy-agent-sandbox:local"

    def __init__(self, project_root: Path, *, command_runner=None, path_mapping=None, **kwargs):
        super().__init__(project_root, **kwargs)
        self.path_mapping = path_mapping
        self._run_command = command_runner or _subprocess_runner
        self._container_name = None

    async def _start_backend(self, session_id: str) -> SandboxStatus:
        await self._require_docker()
        await self._ensure_image()
        container_name = f"yy-agent-{_container_fragment(session_id)}-{uuid4().hex[:8]}"
        result = await self._run_command(self._docker_run_arguments(container_name), 30)
        if result.returncode != 0:
            raise RuntimeError(f"Docker 沙箱启动失败：{_result_message(result)}")
        self._container_name = container_name
        return SandboxStatus(mode="docker", bash_available=True, message="Docker 沙箱已启动，Bash 可用")

    async def _execute_shell(self, command: str, timeout_seconds: int) -> CommandResult:
        arguments = [
            "docker", "exec", "--workdir", self._workspace_target, self._require_container(),
            "timeout", "--signal=KILL", f"{timeout_seconds}s",
            "bash", "--noprofile", "--norc", "-c", command,
        ]
        try:
            return await self._run_command(arguments, timeout_seconds + 5)
        except BaseException:
            # Stopping docker exec alone does not stop its container children.
            await self._close_unlocked()
            raise

    async def _close_backend(self) -> None:
        container, self._container_name = self._container_name, None
        if container is not None:
            result = await self._run_command(["docker", "rm", "--force", container], 30)
            if result.returncode != 0 and "No such container" not in result.stderr:
                raise RuntimeError(f"Docker 沙箱关闭失败：{_result_message(result)}")

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
        workspace_target = self._workspace_target
        mount = f"type=bind,source={self.project_root},target={workspace_target}"
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
                f"{workspace_target}/{relative}:rw,noexec,nosuid,nodev,size=16m",
            ])
        blank = self.state_root / ".yy" / "sandbox" / "empty-secret"
        blank.parent.mkdir(parents=True, exist_ok=True)
        blank.touch(exist_ok=True)
        for path in _environment_files(self.project_root):
            relative = PurePosixPath(path.relative_to(self.project_root).as_posix())
            arguments.extend([
                "--mount",
                f"type=bind,source={blank},target={workspace_target}/{relative},readonly",
            ])
        if self.path_mapping is not None and self.path_mapping.agent_source_root == self.project_root:
            arguments.extend([
                "--mount",
                f"type=bind,source={self.project_root},target=/yy/agent-source",
            ])
            if self.path_mapping.skills_root.is_dir():
                arguments.extend([
                    "--mount",
                    f"type=bind,source={self.path_mapping.skills_root},target=/yy/skills,readonly",
                ])
                arguments.extend(self._alias_carveouts(self.path_mapping.skills_root, "/yy/skills", blank))
            if self.path_mapping.hooks_root.is_dir():
                arguments.extend([
                    "--mount",
                    f"type=bind,source={self.path_mapping.hooks_root},target=/yy/hooks,readonly",
                ])
                arguments.extend(self._alias_carveouts(self.path_mapping.hooks_root, "/yy/hooks", blank))
            # The source alias must not re-expose paths hidden under the
            # Workspace mount.  Docker cannot create a relative alias without
            # image cooperation, so repeat the same carveouts here.
            for relative in (".git", ".yy", ".venv", ".agents", ".codex"):
                arguments.extend([
                    "--tmpfs",
                    f"/yy/agent-source/{relative}:rw,noexec,nosuid,nodev,size=16m",
                ])
            for path in _environment_files(self.project_root):
                relative = PurePosixPath(path.relative_to(self.project_root).as_posix())
                arguments.extend([
                    "--mount",
                    f"type=bind,source={blank},target=/yy/agent-source/{relative},readonly",
                ])
        arguments.append(self.image)
        return arguments

    @staticmethod
    def _alias_carveouts(root: Path, target: str, blank: Path) -> list[str]:
        arguments: list[str] = []
        for relative in (".git", ".yy", ".venv", ".agents", ".codex"):
            if (root / relative).exists():
                arguments.extend([
                    "--tmpfs",
                    f"{target}/{relative}:rw,noexec,nosuid,nodev,size=16m",
                ])
        for path in _environment_files(root):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            arguments.extend([
                "--mount",
                f"type=bind,source={blank},target={target}/{relative},readonly",
            ])
        return arguments

    @property
    def _workspace_target(self) -> str:
        # Preserve the public standalone Sandbox compatibility surface while
        # all Runtime-created sessions use the new logical namespace.
        return "/yy/workspace" if self.path_mapping is not None else "/workspace"

    def _require_container(self) -> str:
        if self._container_name is None:
            raise BashUnavailableError(
                "Docker 沙箱尚未启动或已经关闭；Bash 不会回退到宿主机 Shell",
            )
        return self._container_name

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
