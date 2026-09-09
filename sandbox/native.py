"""OS sandbox backends. All model-authored commands pass through one policy boundary."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .models import SandboxStatus
from .policy import NativePolicy
from .process import run_isolated
from .session import CheckpointSandboxSession, CommandResult, SandboxUnavailableError, SandboxRecoveryRequired


def find_bubblewrap() -> str | None:
    # The confinement helper must never resolve to a workspace/PATH replacement.
    return next((p for p in ("/usr/bin/bwrap", "/bin/bwrap") if Path(p).is_file()), None)


def shell_argv(command: str, shell: str, platform: str) -> list[str]:
    if platform == "win32" and Path(shell).name.lower() in {"powershell.exe", "pwsh.exe"}:
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    return [shell, "--noprofile", "--norc", "-c", command]


def discover_shell(platform: str, configured: str | None = None) -> str:
    if configured:
        shell = shutil.which(configured)
    elif platform == "win32":
        # Do not use bash.exe from System32 (WSL launcher crosses the native boundary).
        shell = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    else:
        shell = shutil.which("bash")
    if not shell or not Path(shell).is_file():
        raise SandboxUnavailableError("No supported shell installed", reason_code="shell_missing")
    name = Path(shell).name.lower()
    if platform == "win32" and (name not in {"bash.exe", "powershell.exe", "pwsh.exe"} or "system32\\bash.exe" in shell.lower()):
        raise SandboxUnavailableError("Use native PowerShell or Git Bash, not WSL/cmd launchers", reason_code="unsupported_shell")
    return str(Path(shell).resolve())


def linux_arguments(policy: NativePolicy, executable: str, command: list[str]) -> list[str]:
    args = [executable, "--unshare-all", "--unshare-user", "--unshare-pid", "--unshare-net",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--disable-userns", "--assert-userns-disabled"]
    # No host home, agent state, sockets or credentials are exposed by default.
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/alternatives", "/etc/localtime"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
    for path in policy.readable_roots:
        args += ["--ro-bind", str(path), str(path)]
    args += ["--bind", str(policy.workspace), str(policy.workspace)]
    for path, hidden in policy.protected_paths():
        if hidden and path.is_dir():
            args += ["--tmpfs", str(path), "--chmod", "000", str(path)]
        elif hidden:
            args += ["--ro-bind", "/dev/null", str(path)]
        else:
            args += ["--ro-bind", str(path), str(path)]
    return args + ["--chdir", str(policy.workspace), "--", *command]


def seatbelt_profile(policy: NativePolicy, temporary: Path) -> str:
    literal = lambda path: json.dumps(str(path), ensure_ascii=False)
    rows = ["(version 1)", "(deny default)", "(allow process-exec)", "(allow process-fork)",
            "(allow signal (target same-sandbox))", "(allow process-info* (target same-sandbox))",
            "(allow sysctl-read)", '(allow file-write-data (literal "/dev/null"))',
            '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))']
    for path in (Path("/System"), Path("/Library"), Path("/usr"), Path("/bin"), Path("/sbin"),
                 Path("/private/etc/paths"), Path("/private/etc/localtime"),
                 *policy.readable_roots, policy.workspace, temporary):
        rows.append(f"(allow file-read* (subpath {literal(path)}))")
    for path in (policy.workspace, temporary):
        rows.append(f"(allow file-write* (subpath {literal(path)}))")
    for path, hidden in policy.protected_paths():
        rows.append(f"(deny file-write* (subpath {literal(path)}))")
        if hidden:
            rows.append(f"(deny file-read* (subpath {literal(path)}))")
    # No network or generic Mach IPC allowance. Descendants inherit Seatbelt.
    rows.append("(deny network*)")
    return "\n".join(rows)


class NativeSandboxSession(CheckpointSandboxSession):
    def __init__(self, project_root: Path, *, readable_roots=(), shell: str | None = None, **kwargs):
        super().__init__(project_root, **kwargs)
        self.policy = NativePolicy(self.project_root, tuple(Path(p).resolve() for p in readable_roots))
        self.platform = sys.platform
        self.configured_shell = shell
        self.shell = ""
        self._temporary = None
        self._windows = None

    async def _start_backend(self, session_id: str) -> SandboxStatus:
        del session_id
        self.policy.validate()
        self.policy.protected_paths()
        self.shell = discover_shell(self.platform, self.configured_shell)
        if self.platform not in {"linux", "darwin", "win32"}:
            raise SandboxUnavailableError("Unsupported OS", reason_code="unsupported_platform")
        self._temporary = tempfile.TemporaryDirectory(prefix="yy-shell-")
        try:
            if self.platform == "win32":
                from .windows import AppContainerRunner
                self._windows = AppContainerRunner(self.policy, self.state_root)
            result = await self._execute_shell("echo yy-sandbox-ready", 30)
            if result.returncode or "yy-sandbox-ready" not in result.stdout:
                raise SandboxUnavailableError("OS sandbox self-test failed", reason_code="os_sandbox_probe_failed")
        except asyncio.CancelledError:
            await self._close_backend()
            raise
        except SandboxRecoveryRequired:
            await self._close_backend()
            raise
        except Exception as exc:
            await self._close_backend()
            if isinstance(exc, SandboxUnavailableError):
                raise
            raise SandboxUnavailableError(f"OS sandbox unavailable: {type(exc).__name__}", reason_code="os_sandbox_probe_failed") from exc
        backend = {"linux": "bubblewrap", "darwin": "seatbelt", "win32": "appcontainer"}[self.platform]
        return SandboxStatus(mode="os", bash_available=True, backend=backend,
                             shell=Path(self.shell).name, message=f"OS sandbox: {backend}; shell: {Path(self.shell).name}; network disabled")

    async def _execute_shell(self, command: str, timeout_seconds: int) -> CommandResult:
        self.policy.validate()
        self.policy.protected_paths()
        if self.platform == "win32" and Path(self.shell).name.lower() in {"powershell.exe", "pwsh.exe"}:
            # PowerShell's provider location can differ from CreateProcess' cwd
            # when starting in AppContainer. Set it explicitly, failing closed.
            location = str(self.project_root).replace("'", "''")
            command = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                       "try { New-PSDrive -Name YYWorkspace -PSProvider FileSystem "
                       f"-Root '{location}' -ErrorAction Stop | Out-Null; "
                       "Set-Location YYWorkspace: -ErrorAction Stop } catch { Write-Error $_; exit 125 }; " + command)
        argv = shell_argv(command, self.shell, self.platform)
        if self.platform == "win32":
            return await self._windows.run(argv, timeout_seconds)
        temporary = Path(self._temporary.name).resolve()
        if self.platform == "linux":
            executable = find_bubblewrap()
            if not executable:
                raise SandboxUnavailableError("Install bubblewrap with user namespaces enabled", reason_code="bubblewrap_missing")
            argv = linux_arguments(self.policy, executable, argv)
            temp = "/tmp"
        else:
            executable = "/usr/bin/sandbox-exec"
            if not Path(executable).is_file():
                raise SandboxUnavailableError("Seatbelt sandbox-exec unavailable", reason_code="seatbelt_missing")
            argv = [executable, "-p", seatbelt_profile(self.policy, temporary), "--", *argv]
            temp = str(temporary)
        # Explicit environment: no Provider keys, proxy, BASH_ENV, PYTHONPATH or startup hooks.
        env = {"PATH": os.defpath, "HOME": temp, "TMPDIR": temp, "TMP": temp, "TEMP": temp,
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "UV_OFFLINE": "1",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        tool_dirs = [str(p / "bin") for p in self.policy.readable_roots if (p / "bin").is_dir()]
        tool_dirs += [str(self.policy.workspace / ".venv/bin")]
        env["PATH"] = os.pathsep.join([*tool_dirs, str(Path(self.shell).parent), os.defpath])
        return await run_isolated(argv, cwd=str(self.project_root), env=env, timeout=timeout_seconds)

    async def _close_backend(self) -> None:
        self._windows = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
