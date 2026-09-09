"""One backend selector for interactive and Harness runtimes."""
from __future__ import annotations

from pathlib import Path
import sys

from .docker import DockerSandboxSession, probe_docker_status
from .native import NativeSandboxSession, discover_shell, find_bubblewrap
from .models import SandboxStatus
from .session import SandboxUnavailableError


def create_sandbox_session(config, *, project_root: Path | None = None, file_locks=None):
    options = dict(state_root=config.agent_root, checkpoint_limit=config.sandbox_checkpoint_limit,
                   file_locks=file_locks)
    root = project_root or config.workspace_root
    if config.sandbox_backend == "docker":
        return DockerSandboxSession(root, **options)
    if config.sandbox_backend != "os":
        raise ValueError("Unsupported sandbox backend")
    return NativeSandboxSession(root, readable_roots=config.sandbox_readable_roots,
                                shell=config.sandbox_shell, **options)


async def probe_sandbox_status(config) -> SandboxStatus:
    """Health is discovery only; TRACE_START performs the actual confined self-test.

    A health request must not create checkpoints, containers, ACL grants or native
    processes. Until tested by a Trace, report pending rather than claim isolation.
    """
    if config.sandbox_backend == "docker":
        return await probe_docker_status()
    try:
        shell = discover_shell(sys.platform, config.sandbox_shell)
        if sys.platform == "linux" and not find_bubblewrap():
            raise SandboxUnavailableError("Install bubblewrap", reason_code="bubblewrap_missing")
        if sys.platform == "darwin" and not Path("/usr/bin/sandbox-exec").is_file():
            raise SandboxUnavailableError("Seatbelt unavailable", reason_code="seatbelt_missing")
        if sys.platform not in {"linux", "darwin", "win32"}:
            raise SandboxUnavailableError("Unsupported OS", reason_code="unsupported_platform")
    except SandboxUnavailableError as exc:
        return SandboxStatus(mode="checkpoint_only", bash_available=False,
                             reason_code=exc.reason_code, message=str(exc))
    return SandboxStatus(mode="pending", bash_available=False, shell=Path(shell).name,
                         backend={"linux": "bubblewrap", "darwin": "seatbelt", "win32": "appcontainer"}[sys.platform],
                         reason_code="trace_probe_required", message="OS backend located; each Trace must pass the sandbox self-test")
