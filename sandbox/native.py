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
from .path_mapping import LogicalRoot, PathMappingSnapshot
from .policy import NativePolicy
from .scan_cache import WorkspaceScanCache
from .process import run_isolated
from .session import (
    BashUnavailableError,
    CheckpointSandboxSession,
    CommandResult,
    SandboxRecoveryRequired,
    SandboxUnavailableError,
)


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


def linux_arguments(
    policy: NativePolicy,
    executable: str,
    command: list[str],
    *,
    protected_paths: tuple[tuple[Path, bool], ...] | None = None,
    writable_roots: tuple[Path, ...] | None = None,
    path_mapping: PathMappingSnapshot | None = None,
) -> list[str]:
    args = [executable, "--unshare-all", "--unshare-user", "--unshare-pid", "--unshare-net",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--disable-userns", "--assert-userns-disabled"]
    # No host home, agent state, sockets or credentials are exposed by default.
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/alternatives", "/etc/localtime"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
    if path_mapping is None:
        for path in policy.readable_roots:
            args += ["--ro-bind", str(path), str(path)]
    else:
        args += ["--dir", "/yy", "--dir", "/yy/read-only"]
        for index, path in enumerate(policy.readable_roots):
            args += ["--ro-bind", str(path), f"/yy/read-only/{index}"]
    selected = writable_roots or (policy.workspace,)
    workspace_target = (
        path_mapping.shell_root(LogicalRoot.WORKSPACE)
        if path_mapping is not None else str(policy.workspace)
    )
    if selected == (policy.workspace,):
        args += ["--bind", str(policy.workspace), workspace_target]
    else:
        args += ["--ro-bind", str(policy.workspace), workspace_target]
        for path in selected:
            target = (
                f"{workspace_target}/{path.relative_to(policy.workspace).as_posix()}"
                if path_mapping is not None else str(path)
            )
            args += ["--bind", str(path), target]
    if path_mapping is not None and path_mapping.agent_source_root == policy.workspace:
        # Aliases point back into the already-carved Workspace mount.  A second
        # bind of the host directory would bypass .git/.env/.venv overlays.
        args += ["--symlink", "workspace", path_mapping.shell_root(LogicalRoot.AGENT_SOURCE)]
        if path_mapping.skills_root.is_dir():
            args += ["--symlink", "agent-source/skills", path_mapping.shell_root(LogicalRoot.SKILLS)]
        if path_mapping.hooks_root.is_dir():
            args += ["--symlink", "agent-source/extension/hook", path_mapping.shell_root(LogicalRoot.HOOKS)]
    for path, hidden in protected_paths if protected_paths is not None else policy.protected_paths():
        if hidden and path.is_dir():
            target = _sandbox_target(path, policy.workspace, workspace_target)
            args += ["--tmpfs", target, "--chmod", "000", target]
        elif hidden:
            args += ["--ro-bind", "/dev/null", _sandbox_target(path, policy.workspace, workspace_target)]
        else:
            args += ["--ro-bind", str(path), _sandbox_target(path, policy.workspace, workspace_target)]
    return args + ["--chdir", workspace_target, "--", *command]


def _sandbox_target(path: Path, workspace: Path, workspace_target: str) -> str:
    if workspace_target == str(workspace):
        return str(path)
    if path == workspace:
        return workspace_target
    if path.is_relative_to(workspace):
        return f"{workspace_target}/{path.relative_to(workspace).as_posix()}"
    return str(path)


def seatbelt_profile(
    policy: NativePolicy,
    temporary: Path,
    *,
    protected_paths: tuple[tuple[Path, bool], ...] | None = None,
    writable_roots: tuple[Path, ...] | None = None,
) -> str:
    literal = lambda path: json.dumps(str(path), ensure_ascii=False)
    rows = ["(version 1)", "(deny default)", "(allow process-exec)", "(allow process-fork)",
            "(allow signal (target same-sandbox))", "(allow process-info* (target same-sandbox))",
            "(allow sysctl-read)", '(allow file-write-data (literal "/dev/null"))',
            '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))']
    for path in (Path("/System"), Path("/Library"), Path("/usr"), Path("/bin"), Path("/sbin"),
                 Path("/private/etc/paths"), Path("/private/etc/localtime"),
                 *policy.readable_roots, policy.workspace, temporary):
        rows.append(f"(allow file-read* (subpath {literal(path)}))")
    for path in (*(writable_roots or (policy.workspace,)), temporary):
        rows.append(f"(allow file-write* (subpath {literal(path)}))")
    for path, hidden in protected_paths if protected_paths is not None else policy.protected_paths():
        rows.append(f"(deny file-write* (subpath {literal(path)}))")
        if hidden:
            rows.append(f"(deny file-read* (subpath {literal(path)}))")
    # No network or generic Mach IPC allowance. Descendants inherit Seatbelt.
    rows.append("(deny network*)")
    return "\n".join(rows)


class NativeSandboxSession(CheckpointSandboxSession):
    def __init__(self, project_root: Path, *, readable_roots=(), shell: str | None = None, path_mapping=None, **kwargs):
        super().__init__(project_root, **kwargs)
        self.policy = NativePolicy(self.project_root, tuple(Path(p).resolve() for p in readable_roots))
        self.path_mapping = path_mapping
        self.platform = sys.platform
        self.configured_shell = shell
        self.shell = ""
        self._temporary = None
        self._windows = None
        self._protected_snapshot: tuple[tuple[Path, bool], ...] | None = None
        self._backend_ready = False
        self._backend_dirty = False
        self._current_writable_roots: tuple[Path, ...] = (self.project_root,)
        self._lease_writable_roots: tuple[Path, ...] = ()
        backend = {"linux": "bubblewrap", "darwin": "seatbelt", "win32": "appcontainer"}.get(
            self.platform, self.platform,
        )
        self.scan_cache = WorkspaceScanCache(
            self.project_root, self.state_root, backend=backend,
        )

    async def _start_backend(self, session_id: str) -> SandboxStatus:
        del session_id
        self.policy.validate()
        self.shell = discover_shell(self.platform, self.configured_shell)
        if self.platform not in {"linux", "darwin", "win32"}:
            raise SandboxUnavailableError("Unsupported OS", reason_code="unsupported_platform")
        self._temporary = tempfile.TemporaryDirectory(prefix="yy-shell-")
        if self.platform == "win32":
            from .windows import AppContainerRunner
            self._windows = AppContainerRunner(self.policy, self.state_root)
        backend = {"linux": "bubblewrap", "darwin": "seatbelt", "win32": "appcontainer"}[self.platform]
        return SandboxStatus(
            mode="os_lazy", bash_available=True, backend=backend,
            shell=Path(self.shell).name,
            reason_code="sandbox_lazy_start",
            message=f"OS sandbox ready on first Shell use: {backend}; network disabled",
        )

    async def _ensure_backend_ready(self) -> None:
        if self._backend_ready and not self._backend_dirty:
            # Editors and build tools may mutate the workspace outside this
            # Runtime. Revalidate the durable workspace fingerprint before
            # reusing permissions; a cache hit only stats directories and Git.
            try:
                current = self.scan_cache.protected_paths(self.policy)
            except Exception as exc:
                await self._close_backend()
                reason = exc.reason_code if isinstance(exc, SandboxUnavailableError) else "workspace_revalidation_failed"
                self._status = SandboxStatus(
                    mode="checkpoint_only", bash_available=False,
                    reason_code=reason,
                    message=f"Workspace safety revalidation failed: {str(exc) or type(exc).__name__}",
                )
                raise BashUnavailableError(self._status.message) from exc
            if self._temporary is None and self._windows is None:
                # Test/injected native backends declare themselves ready and
                # own no permission lease that needs rebuilding.
                self._protected_snapshot = current
                return
            if self._protected_snapshot is None and self.scan_cache.last_cache_hit:
                self._protected_snapshot = current
                return
            if self.scan_cache.last_cache_hit and current == self._protected_snapshot:
                return
            if self._windows is not None:
                await self._windows.close()
            self._backend_ready = False
            self._protected_snapshot = current
            self._status = self._status.model_copy(update={
                "mode": "os_lazy",
                "reason_code": "workspace_changed_externally",
                "message": "Workspace safety projection changed; refreshing OS sandbox permissions",
            })
        if self._backend_dirty:
            if self._windows is not None:
                await self._windows.close()
            self._backend_ready = False
            self._backend_dirty = False
            self._protected_snapshot = None
            self._status = self._status.model_copy(update={
                "mode": "os_lazy",
                "reason_code": "workspace_changed",
                "message": "Workspace changed; OS sandbox permissions will be refreshed on use",
            })
        # Injected/test backends may declare themselves fully active at
        # TRACE_START. Only the native ``os_lazy`` state needs the first-use
        # probe below.
        if self._status.mode == "os":
            if self._protected_snapshot is None:
                self._protected_snapshot = self.scan_cache.protected_paths(self.policy)
            self._backend_ready = True
            return
        if self._status.mode != "os_lazy":
            return
        try:
            self._protected_snapshot = self.scan_cache.protected_paths(self.policy)
            result = await self._execute_shell("echo yy-sandbox-ready", 30)
            if result.returncode or "yy-sandbox-ready" not in result.stdout:
                raise SandboxUnavailableError(
                    "OS sandbox self-test failed", reason_code="os_sandbox_probe_failed",
                )
        except asyncio.CancelledError:
            await self._close_backend()
            raise
        except SandboxRecoveryRequired:
            await self._close_backend()
            raise
        except Exception as exc:
            await self._close_backend()
            reason = exc.reason_code if isinstance(exc, SandboxUnavailableError) else "os_sandbox_probe_failed"
            self._status = SandboxStatus(
                mode="checkpoint_only", bash_available=False, reason_code=reason,
                message=f"OS sandbox unavailable: {str(exc) or type(exc).__name__}；Bash/Shell 已禁用",
            )
            raise BashUnavailableError(self._status.message) from exc
        self._backend_ready = True
        self._status = SandboxStatus(
            mode="os", bash_available=True,
            backend={"linux": "bubblewrap", "darwin": "seatbelt", "win32": "appcontainer"}[self.platform],
            shell=Path(self.shell).name,
            message=f"OS sandbox active; shell: {Path(self.shell).name}; network disabled",
        )

    async def _execute_shell(self, command: str, timeout_seconds: int) -> CommandResult:
        self.policy.validate()
        protected = self._protected_snapshot
        if protected is None:
            protected = self.scan_cache.protected_paths(self.policy)
            self._protected_snapshot = protected
        if self.platform == "win32" and Path(self.shell).name.lower() in {"powershell.exe", "pwsh.exe"}:
            # PowerShell's provider location can differ from CreateProcess' cwd
            # when starting in AppContainer. Set it explicitly, failing closed.
            location = str(self.project_root).replace("'", "''")
            command = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                       "try { New-PSDrive -Name YYWorkspace -PSProvider FileSystem "
                       f"-Root '{location}' -ErrorAction Stop | Out-Null; "
                       + self._windows_source_drives()
                       + "Set-Location YYWorkspace: -ErrorAction Stop } catch { Write-Error $_; exit 125 }; " + command)
        elif self.platform == "darwin" and self.path_mapping is not None:
            # Seatbelt provides the security boundary but no mount namespace.
            # Translate only the fixed logical roots before invoking the shell;
            # model-visible output is projected back after execution.
            command = self.path_mapping.translate_posix_command(command)
        argv = shell_argv(command, self.shell, self.platform)
        if self.platform == "win32":
            return await self._windows.run(
                argv, timeout_seconds, protected_paths=protected,
                writable_roots=self._lease_writable_roots or (self.project_root,),
            )
        temporary = Path(self._temporary.name).resolve()
        if self.platform == "linux":
            executable = find_bubblewrap()
            if not executable:
                raise SandboxUnavailableError("Install bubblewrap with user namespaces enabled", reason_code="bubblewrap_missing")
            argv = linux_arguments(
                self.policy, executable, argv, protected_paths=protected,
                writable_roots=self._current_writable_roots,
                path_mapping=self.path_mapping,
            )
            temp = "/tmp"
        else:
            executable = "/usr/bin/sandbox-exec"
            if not Path(executable).is_file():
                raise SandboxUnavailableError("Seatbelt sandbox-exec unavailable", reason_code="seatbelt_missing")
            argv = [
                executable, "-p",
                seatbelt_profile(
                    self.policy, temporary, protected_paths=protected,
                    writable_roots=self._current_writable_roots,
                ),
                "--", *argv,
            ]
            temp = str(temporary)
        # Explicit environment: no Provider keys, proxy, BASH_ENV, PYTHONPATH or startup hooks.
        env = {"PATH": os.defpath, "HOME": temp, "TMPDIR": temp, "TMP": temp, "TEMP": temp,
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "UV_OFFLINE": "1",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        if self.platform == "linux" and self.path_mapping is not None:
            tool_dirs = [f"/yy/read-only/{index}/bin" for index, p in enumerate(self.policy.readable_roots) if (p / "bin").is_dir()]
            tool_dirs += ["/yy/workspace/.venv/bin"]
        else:
            tool_dirs = [str(p / "bin") for p in self.policy.readable_roots if (p / "bin").is_dir()]
            tool_dirs += [str(self.policy.workspace / ".venv/bin")]
        env["PATH"] = os.pathsep.join([*tool_dirs, str(Path(self.shell).parent), os.defpath])
        return await run_isolated(argv, cwd=str(self.project_root), env=env, timeout=timeout_seconds)

    def _windows_source_drives(self) -> str:
        mapping = self.path_mapping
        if mapping is None or mapping.agent_source_root != self.project_root:
            return ""
        values = (
            ("YYAgentSource", mapping.agent_source_root),
            ("YYSkills", mapping.skills_root),
            ("YYHooks", mapping.hooks_root),
        )
        commands = []
        for name, path in values:
            if path.is_dir():
                location = str(path).replace("'", "''")
                commands.append(
                    f"New-PSDrive -Name {name} -PSProvider FileSystem -Root '{location}' -ErrorAction Stop | Out-Null; "
                )
        return "".join(commands)

    def _configure_bash_access(self, writable_paths: tuple[str, ...] | None) -> None:
        normalized = writable_paths
        if writable_paths is not None and self.path_mapping is not None:
            normalized = tuple(
                self.path_mapping.resolve_workspace_path(value)
                .relative_to(self.project_root)
                .as_posix()
                or "."
                for value in writable_paths
            )
        selected = self.policy.writable_roots(normalized)
        self._current_writable_roots = selected
        if self.platform != "win32":
            return
        combined = [*self._lease_writable_roots, *selected]
        expanded: list[Path] = []
        for path in sorted(set(combined), key=lambda item: (len(item.parts), str(item))):
            if not any(path == parent or path.is_relative_to(parent) for parent in expanded):
                expanded.append(path)
        value = tuple(expanded)
        if value != self._lease_writable_roots and self._backend_ready:
            self._backend_dirty = True
        self._lease_writable_roots = value

    async def _close_backend(self) -> None:
        self._backend_ready = False
        self._backend_dirty = False
        self._protected_snapshot = None
        if self._windows is not None:
            await self._windows.close()
        self._windows = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _workspace_changed(self) -> None:
        self._protected_snapshot = None
        self.scan_cache.invalidate()
        # A Windows AppContainer lease contains temporary ACL grants based on
        # the exact scanned tree. Keep it through read-only commands, but
        # rebuild it before the next command after any durable tree mutation.
        if self._windows is not None:
            self._backend_dirty = True
