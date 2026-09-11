"""Native Windows AppContainer + Job Object launcher (no network capabilities).

ACL entries use a fresh package SID per sandbox trace. A durable lease is
written BEFORE touching ACLs; recovery removes only that SID, never restores an
old DACL over a user's edits. Job handles are non-inheritable and kill descendants
on cancellation, timeout, normal completion, or Gateway process death.
"""
from __future__ import annotations

import asyncio
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from .policy import NativePolicy, is_link
from .session import CommandResult, SandboxRecoveryRequired, SandboxUnavailableError


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]


class STARTUPINFO(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR), ("title", W.LPWSTR),
                ("x", W.DWORD), ("y", W.DWORD), ("xs", W.DWORD), ("ys", W.DWORD),
                ("xc", W.DWORD), ("yc", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD),
                ("show", W.WORD), ("reserved2", W.WORD), ("reserved3", C.c_void_p),
                ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class STARTUPINFOEX(C.Structure):
    _fields_ = [("startup", STARTUPINFO), ("attributes", C.c_void_p)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


class SECURITY_CAPABILITIES(C.Structure):
    _fields_ = [("sid", C.c_void_p), ("capabilities", C.c_void_p), ("count", W.DWORD), ("reserved", W.DWORD)]


class TRUSTEE(C.Structure):
    _fields_ = [("multiple", C.c_void_p), ("operation", C.c_int), ("form", C.c_int),
                ("type", C.c_int), ("name", C.c_void_p)]


class EXPLICIT_ACCESS(C.Structure):
    _fields_ = [("permissions", W.DWORD), ("mode", C.c_int), ("inheritance", W.DWORD), ("trustee", TRUSTEE)]


class BASIC_LIMIT(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong), ("flags", W.DWORD),
                ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("active", W.DWORD),
                ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read", "write", "other")]


class EXTENDED_LIMIT(C.Structure):
    _fields_ = [("basic", BASIC_LIMIT), ("io", IO_COUNTERS), ("process_memory", C.c_size_t),
                ("job_memory", C.c_size_t), ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class BASIC_ACCOUNTING(C.Structure):
    _fields_ = [(name, C.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
        (name, W.DWORD) for name in ("page_faults", "total", "active", "terminated")
    ]


class WinAPI:
    def __init__(self):
        if os.name != "nt":
            raise OSError("AppContainer is available only on Windows")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.userenv = C.WinDLL("userenv", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        P = C.c_void_p
        # Never rely on ctypes' default int return for 64-bit HANDLEs/pointers.
        specs = [
            (self.kernel, "CreateJobObjectW", [P, W.LPCWSTR], W.HANDLE),
            (self.kernel, "SetInformationJobObject", [W.HANDLE, C.c_int, P, W.DWORD], W.BOOL),
            (self.kernel, "QueryInformationJobObject", [W.HANDLE, C.c_int, P, W.DWORD, P], W.BOOL),
            (self.kernel, "AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
            (self.kernel, "TerminateJobObject", [W.HANDLE, W.UINT], W.BOOL),
            (self.kernel, "TerminateProcess", [W.HANDLE, W.UINT], W.BOOL),
            (self.kernel, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.kernel, "ResumeThread", [W.HANDLE], W.DWORD),
            (self.kernel, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            (self.kernel, "GetExitCodeProcess", [W.HANDLE, P], W.BOOL),
            (self.kernel, "InitializeProcThreadAttributeList", [P, W.DWORD, W.DWORD, P], W.BOOL),
            (self.kernel, "UpdateProcThreadAttribute", [P, W.DWORD, C.c_size_t, P, C.c_size_t, P, P], W.BOOL),
            (self.kernel, "DeleteProcThreadAttributeList", [P], None),
            (self.kernel, "CreateProcessW", [W.LPCWSTR, W.LPWSTR, P, P, W.BOOL, W.DWORD, P, W.LPCWSTR, P, P], W.BOOL),
            (self.kernel, "CreatePipe", [P, P, P, W.DWORD], W.BOOL),
            (self.kernel, "SetHandleInformation", [W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
            (self.kernel, "ReadFile", [W.HANDLE, P, W.DWORD, P, P], W.BOOL),
            (self.kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, P, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
            (self.kernel, "LocalFree", [P], P),
            (self.userenv, "CreateAppContainerProfile", [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, P, W.DWORD, P], C.c_long),
            (self.userenv, "DeleteAppContainerProfile", [W.LPCWSTR], C.c_long),
            (self.userenv, "DeriveAppContainerSidFromAppContainerName", [W.LPCWSTR, P], C.c_long),
            (self.advapi, "FreeSid", [P], P),
            (self.advapi, "GetNamedSecurityInfoW", [W.LPWSTR, C.c_int, W.DWORD, P, P, P, P, P], W.DWORD),
            (self.advapi, "SetEntriesInAclW", [W.DWORD, P, P, P], W.DWORD),
            (self.advapi, "SetNamedSecurityInfoW", [W.LPWSTR, C.c_int, W.DWORD, P, P, P, P], W.DWORD),
        ]
        for library, name, args, result in specs:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def check(result):
        if not result:
            raise C.WinError(C.get_last_error())
        return result

    def acl(self, path: Path, sid, *, mode: int, permissions: int = 0, inherit: bool = True) -> None:
        if is_link(path):
            raise OSError("Refuse ACL mutation on reparse point")
        dacl, descriptor, updated = C.c_void_p(), C.c_void_p(), C.c_void_p()
        code = self.advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, C.byref(dacl), None, C.byref(descriptor))
        if code:
            raise C.WinError(code)
        try:
            entry = EXPLICIT_ACCESS(permissions, mode, 3 if inherit and path.is_dir() else 0, TRUSTEE(None, 0, 0, 5, sid))
            code = self.advapi.SetEntriesInAclW(1, C.byref(entry), dacl, C.byref(updated))
            if code:
                raise C.WinError(code)
            code = self.advapi.SetNamedSecurityInfoW(str(path), 1, 4, None, None, updated, None)
            if code:
                raise C.WinError(code)
        finally:
            self.kernel.LocalFree(updated)
            self.kernel.LocalFree(descriptor)

    def check_acl_access(self, path: Path) -> None:
        """Read-only access probe before any profile/permission side effects.

        MODIFY alone does not include WRITE_DAC, notably for virtualenvs created
        by a different Windows account. Do not take ownership or elevate here.
        """
        if is_link(path):
            raise SandboxUnavailableError(f"ACL target is a reparse point: {path}", reason_code="unsafe_workspace_entry")
        handle = self.kernel.CreateFileW(
            str(path), 0x60000, 7, None, 3, 0x02200000, None,
        )  # READ_CONTROL | WRITE_DAC; directory + open-reparse-point, no writes
        if handle == C.c_void_p(-1).value:
            code = C.get_last_error()
            raise SandboxUnavailableError(
                f"Windows AppContainer needs READ_CONTROL/WRITE_DAC on {path} (WinError {code}). "
                "Use a workspace/toolchain owned by the current user; no automatic ownership or permission override.",
                reason_code="windows_acl_permission_denied",
            )
        self.kernel.CloseHandle(handle)

    def stop_job(self, job) -> None:
        try:
            self.check(self.kernel.TerminateJobObject(job, 124))
            deadline = time.monotonic() + 10
            while True:
                info = BASIC_ACCOUNTING()
                self.check(self.kernel.QueryInformationJobObject(job, 1, C.byref(info), C.sizeof(info), None))
                if info.active == 0:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Sandbox job still has active processes")
                time.sleep(0.02)
        except Exception as exc:
            raise SandboxRecoveryRequired("Cannot confirm sandbox job termination") from exc


class AppContainerRunner:
    def __init__(self, policy: NativePolicy, state_root: Path):
        self.policy = policy
        self.leases = state_root / ".yy/sandbox/native-leases"
        self.leases.mkdir(parents=True, exist_ok=True)
        self._lease_file = None
        self._lease_path: Path | None = None
        self._lease_record: dict | None = None
        self._recovered = False
        self._lease_recovery_required = False

    async def run(
        self,
        argv: list[str],
        timeout: float,
        *,
        protected_paths: tuple[tuple[Path, bool], ...] | None = None,
        writable_roots: tuple[Path, ...] | None = None,
    ) -> CommandResult:
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(
            self._run, argv, timeout, cancelled, protected_paths, writable_roots,
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # Do not restore a checkpoint until the OS confirms the job was terminated.
            await asyncio.shield(task)
            raise

    async def close(self) -> None:
        """Release the trace-scoped AppContainer profile and temporary ACLs."""
        await asyncio.to_thread(self._close)

    def recover(self, api: WinAPI) -> None:
        import msvcrt
        for path in self.leases.glob("yy-*.json"):
            with path.open("r+b") as file:
                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    continue  # another live command owns this lease
                try:
                    record = json.loads(file.read().decode("utf-8"))
                    if record["name"] != path.stem or not path.stem.startswith("yy-"):
                        raise OSError("Invalid sandbox ACL lease identity")
                    sid = C.c_void_p()
                    code = api.userenv.DeriveAppContainerSidFromAppContainerName(record["name"], C.byref(sid))
                    if code < 0:
                        raise OSError(f"Cannot derive sandbox SID: {code}")
                    try:
                        self._cleanup(api, record, sid)
                    finally:
                        api.advapi.FreeSid(sid)
                finally:
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            path.unlink()

    @staticmethod
    def _cleanup(api: WinAPI, record, sid):
        # Removing grants also removes explicit deny entries for this one unique SID.
        # Never restore the entire saved DACL or touch any other identity's entries.
        for item in reversed(record["paths"]):
            path = Path(item)
            if path.exists():
                api.acl(path, sid, mode=4)  # REVOKE_ACCESS
        code = api.userenv.DeleteAppContainerProfile(record["name"])
        if code < 0 and code != -2147024894:  # profile absent is idempotent
            raise OSError(f"AppContainer profile cleanup failed: {code}")

    def _run(self, argv, timeout, cancelled, protected_paths=None, writable_roots=None):
        api = WinAPI()
        self.policy.validate()
        protected = protected_paths or self.policy.protected_paths()
        record, sid, system = self._ensure_lease(
            api, argv, protected, writable_roots or (self.policy.workspace,),
        )
        name = str(record["name"])
        handles = []
        readers = []
        output = [bytearray(), bytearray()]
        attributes = None
        job = None
        process = PROCESS_INFORMATION()
        try:
            sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), None, True)
            writes = []
            for index in range(2):
                read, write = W.HANDLE(), W.HANDLE()
                api.check(api.kernel.CreatePipe(C.byref(read), C.byref(write), C.byref(sa), 0))
                handles.extend([read.value, write.value])
                api.check(api.kernel.SetHandleInformation(read, 1, 0))
                writes.append(write.value)

                def drain(handle=read.value, target=output[index]):
                    chunk = C.create_string_buffer(65536)
                    count = W.DWORD()
                    while api.kernel.ReadFile(handle, chunk, len(chunk), C.byref(count), None) and count.value:
                        if len(target) < 1000000:
                            target.extend(chunk.raw[:min(count.value, 1000000 - len(target))])
                readers.append(threading.Thread(target=drain, daemon=True))

            stdin = api.kernel.CreateFileW("NUL", 0x80000000, 3, C.byref(sa), 3, 0, None)
            if stdin == C.c_void_p(-1).value:
                raise C.WinError()
            handles.append(stdin)
            size = C.c_size_t()
            api.kernel.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            attributes = C.create_string_buffer(size.value)
            api.check(api.kernel.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)))
            capabilities = SECURITY_CAPABILITIES(sid, None, 0, 0)  # ZERO internet/loopback capabilities
            api.check(api.kernel.UpdateProcThreadAttribute(attributes, 0, 0x20009, C.byref(capabilities), C.sizeof(capabilities), None, None))
            std_handles = (W.HANDLE * 3)(stdin, *writes)
            api.check(api.kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, std_handles, C.sizeof(std_handles), None, None))
            startup = STARTUPINFOEX()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = stdin, *writes
            startup.attributes = C.cast(attributes, C.c_void_p)

            job = api.check(api.kernel.CreateJobObjectW(None, None))
            limits = EXTENDED_LIMIT()
            limits.basic.flags = 0x2000 | 0x8 | 0x200  # KILL_ON_JOB_CLOSE, ACTIVE_PROCESS, JOB_MEMORY
            limits.basic.active = 256
            limits.job_memory = 1024 * 1024 * 1024
            api.check(api.kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
            # CreateProcess' AppContainer setup needs the standard profile variables
            # as well as SystemRoot. This is an allowlist, never the parent's full env.
            env = {key.upper(): value for key, value in os.environ.items() if key.upper() in {
                "USERPROFILE", "LOCALAPPDATA", "APPDATA", "SYSTEMDRIVE", "OS",
                "USERNAME", "USERDOMAIN", "PROCESSOR_ARCHITECTURE", "COMSPEC",
                "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
            }}
            env.update({"SYSTEMROOT": str(system), "WINDIR": str(system),
                   "PATH": os.pathsep.join([str(Path(argv[0]).parent), str(system / "System32"),
                                            str(self.policy.workspace / ".venv/Scripts")]),
                   "UV_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            block = C.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
            command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
            api.check(api.kernel.CreateProcessW(argv[0], command, None, None, True,
                                               0x80000 | 0x400 | 0x4 | 0x08000000,
                                               block, str(self.policy.workspace), C.byref(startup), C.byref(process)))
            handles.extend([process.process, process.thread])
            try:
                api.check(api.kernel.AssignProcessToJobObject(job, process.process))
            except BaseException:
                api.kernel.TerminateProcess(process.process, 1)
                api.kernel.WaitForSingleObject(process.process, 5000)
                raise
            for handle in writes:
                api.kernel.CloseHandle(handle)
                handles.remove(handle)
            for reader in readers:
                reader.start()
            if api.kernel.ResumeThread(process.thread) == 0xFFFFFFFF:
                raise C.WinError()
            deadline = time.monotonic() + timeout
            timed_out = False
            while True:
                wait_result = api.kernel.WaitForSingleObject(process.process, 50)
                if wait_result == 0:
                    break
                if wait_result != 258:
                    raise SandboxRecoveryRequired("Cannot determine sandbox process exit state")
                if cancelled.is_set() or time.monotonic() >= deadline:
                    timed_out = not cancelled.is_set()
                    api.check(api.kernel.TerminateJobObject(job, 124))
                    api.kernel.WaitForSingleObject(process.process, 5000)
                    break
            exit_code = W.DWORD()
            api.check(api.kernel.GetExitCodeProcess(process.process, C.byref(exit_code)))
            api.stop_job(job)  # no detached background writers before checkpoint/ACL cleanup
            for reader in readers:
                reader.join(timeout=5)
                if reader.is_alive():
                    raise SandboxRecoveryRequired("Sandbox output handle remained active after job termination")
            if timed_out:
                raise TimeoutError("Sandbox command timed out")
            return CommandResult(returncode=exit_code.value,
                                 stdout=output[0].decode("utf-8", errors="replace"),
                                 stderr=output[1].decode("utf-8", errors="replace"))
        finally:
            termination_error = None
            if job:
                try:
                    api.stop_job(job)
                except SandboxRecoveryRequired as exc:
                    termination_error = exc
                    self._lease_recovery_required = True
                finally:
                    api.kernel.CloseHandle(job)
            if attributes is not None:
                api.kernel.DeleteProcThreadAttributeList(attributes)
            for handle in handles:
                api.kernel.CloseHandle(handle)
            api.advapi.FreeSid(sid)
            if termination_error is not None:
                raise termination_error

    def _ensure_lease(self, api: WinAPI, argv, protected, writable_roots):
        """Create ACL grants once and reuse them for this sandbox trace."""
        import msvcrt

        if not self._recovered:
            try:
                self.recover(api)
            except Exception as exc:
                raise SandboxRecoveryRequired(
                    "Previous sandbox permission lease requires recovery",
                ) from exc
            self._recovered = True
        if self._lease_record is not None:
            expected = [str(path) for path in writable_roots]
            if self._lease_record.get("writable_roots") != expected:
                raise SandboxRecoveryRequired(
                    "Active sandbox lease does not match requested write roots",
                )
            sid = C.c_void_p()
            code = api.userenv.DeriveAppContainerSidFromAppContainerName(
                str(self._lease_record["name"]), C.byref(sid),
            )
            if code < 0:
                raise SandboxRecoveryRequired("Cannot recover active sandbox SID")
            system = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
            return self._lease_record, sid, system

        name = "yy-" + uuid4().hex
        sid = C.c_void_p()
        if api.userenv.DeriveAppContainerSidFromAppContainerName(name, C.byref(sid)) < 0:
            raise OSError("Cannot derive AppContainer SID")
        excluded = {path for path, _ in protected}

        def access_entries(root: Path) -> list[tuple[Path, bool]]:
            entries = [(root, not any(path.is_relative_to(root) for path in excluded))]
            if entries[0][1]:
                return entries
            for directory, dirs, files in os.walk(root):
                base = Path(directory)
                dirs[:] = [child for child in dirs if base / child not in excluded]
                for child in list(dirs):
                    path = base / child
                    can_inherit = not any(item.is_relative_to(path) for item in excluded)
                    entries.append((path, can_inherit))
                    if can_inherit:
                        dirs.remove(child)
                entries.extend(
                    (base / child, False)
                    for child in files if base / child not in excluded
                )
            return entries

        # Read access covers the repository, while write access is the explicit
        # per-command/trace subset. Protected paths never receive either ACL.
        workspace_readable = access_entries(self.policy.workspace)
        writable: list[tuple[Path, bool]] = []
        for root in writable_roots:
            writable.extend(access_entries(root))
        external_readable = list(dict.fromkeys([
            *self.policy.readable_roots, Path(argv[0]).parent,
            *(path for path, hidden in protected if not hidden),
        ]))
        shell_root = Path(argv[0]).parent
        if self.policy.workspace.is_relative_to(shell_root) or shell_root.is_relative_to(self.policy.workspace):
            api.advapi.FreeSid(sid)
            raise OSError("Windows shell installation must be outside the mutable workspace")
        paths = list(dict.fromkeys([
            *(path for path, _ in workspace_readable),
            *(path for path, _ in writable),
            *external_readable,
        ]))
        # System binaries already have AppContainer execute permission. Do not edit System32 ACLs.
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
        paths = [p for p in paths if not p.is_relative_to(system)]
        try:
            for path in paths:
                api.check_acl_access(path)
        except BaseException:
            api.advapi.FreeSid(sid)
            raise
        record = {
            "name": name,
            "paths": [str(path) for path in paths],
            "writable_roots": [str(path) for path in writable_roots],
        }
        lease = self.leases / (name + ".json")
        file = lease.open("x+b")
        cleaned = False
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            file.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
            created_sid = C.c_void_p()
            code = api.userenv.CreateAppContainerProfile(name, name, "Yuan Ye isolated shell", None, 0, C.byref(created_sid))
            if code < 0:
                raise OSError(f"CreateAppContainerProfile failed: {code}")
            api.advapi.FreeSid(created_sid)
            for path, inherit in workspace_readable:
                if path in paths:
                    api.acl(path, sid, mode=1, permissions=0x1200A9, inherit=inherit)
            for path in external_readable:
                if path in paths:
                    api.acl(path, sid, mode=1, permissions=0x1200A9)
            for path, inherit in writable:
                api.acl(path, sid, mode=1, permissions=0x1301BF, inherit=inherit)
                api.acl(path, sid, mode=3, permissions=0xC0040, inherit=inherit)
            self._lease_file = file
            self._lease_path = lease
            self._lease_record = record
            return record, sid, system
        except BaseException:
            try:
                self._cleanup(api, record, sid)
                cleaned = True
            finally:
                api.advapi.FreeSid(sid)
                file.close()
                if cleaned:
                    lease.unlink(missing_ok=True)
            raise

    def _close(self) -> None:
        import msvcrt

        file = self._lease_file
        lease = self._lease_path
        record = self._lease_record
        if file is None or lease is None or record is None:
            return
        self._lease_file = None
        self._lease_path = None
        self._lease_record = None
        try:
            if self._lease_recovery_required:
                raise SandboxRecoveryRequired(
                    "Sandbox process cleanup is unconfirmed; ACL lease preserved",
                )
            api = WinAPI()
            sid = C.c_void_p()
            code = api.userenv.DeriveAppContainerSidFromAppContainerName(
                str(record["name"]), C.byref(sid),
            )
            if code < 0:
                raise OSError(f"Cannot derive sandbox SID: {code}")
            try:
                self._cleanup(api, record, sid)
            finally:
                api.advapi.FreeSid(sid)
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            file.close()
            lease.unlink(missing_ok=True)
        except SandboxRecoveryRequired:
            file.close()
            raise
        except Exception as exc:
            file.close()
            raise SandboxRecoveryRequired(
                "Sandbox ACL cleanup failed; lease preserved",
            ) from exc
