"""跨平台、跨进程的异步工作区读写锁。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal


LockMode = Literal["shared", "exclusive"]
_POLL_INTERVAL_SECONDS = 0.05
_LOCAL_LOCKS: dict[str, "_FairAsyncRWLock"] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_WINDOWS_API = None


class _FairAsyncRWLock:
    """进程内写优先读写锁，防止等待中的写协程长期饥饿。"""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def acquire(self, mode: LockMode) -> AsyncIterator[None]:
        if mode == "shared":
            await self._acquire_shared()
            try:
                yield
            finally:
                await self._release_shared()
            return
        await self._acquire_exclusive()
        try:
            yield
        finally:
            await self._release_exclusive()

    async def _acquire_shared(self) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer and self._waiting_writers == 0,
            )
            self._readers += 1

    async def _release_shared(self) -> None:
        async with self._condition:
            self._readers -= 1
            self._condition.notify_all()

    async def _acquire_exclusive(self) -> None:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._writer and self._readers == 0,
                )
                self._writer = True
            finally:
                self._waiting_writers -= 1

    async def _release_exclusive(self) -> None:
        async with self._condition:
            self._writer = False
            self._condition.notify_all()


class _SystemLockHandle:
    """持有一个操作系统文件锁；进程退出时句柄会由系统自动释放。"""

    def __init__(self, path: Path, mode: LockMode) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.mode = mode
        self._file = path.open("a+b")
        self._ensure_lock_byte()
        self._locked = False
        self._windows_overlapped = None

    def try_acquire(self) -> bool:
        try:
            if os.name == "nt":
                self._try_windows_lock()
            else:
                self._try_posix_lock()
        except (BlockingIOError, PermissionError):
            return False
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        self._locked = True
        return True

    def release(self) -> None:
        try:
            if self._locked:
                self._file.seek(0)
                if os.name == "nt":
                    self._release_windows_lock()
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._file.close()

    def close_unlocked(self) -> None:
        self._file.close()

    @property
    def locked(self) -> bool:
        return self._locked

    def _ensure_lock_byte(self) -> None:
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)

    def _try_windows_lock(self) -> None:
        import ctypes
        import msvcrt

        kernel32, overlapped_type = _windows_lock_api()
        overlapped = overlapped_type()
        flags = 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
        if self.mode == "exclusive":
            flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
        handle = msvcrt.get_osfhandle(self._file.fileno())
        succeeded = kernel32.LockFileEx(
            handle,
            flags,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        )
        if not succeeded:
            error = ctypes.get_last_error()
            if error in {33, 158}:  # ERROR_LOCK_VIOLATION / ERROR_NOT_LOCKED
                raise BlockingIOError(error, "文件已被其他进程锁定")
            raise ctypes.WinError(error)
        self._windows_overlapped = overlapped

    def _release_windows_lock(self) -> None:
        import ctypes
        import msvcrt

        kernel32, overlapped_type = _windows_lock_api()
        overlapped = self._windows_overlapped or overlapped_type()
        handle = msvcrt.get_osfhandle(self._file.fileno())
        if not kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._windows_overlapped = None

    def _try_posix_lock(self) -> None:
        import fcntl

        mode = fcntl.LOCK_EX if self.mode == "exclusive" else fcntl.LOCK_SH
        fcntl.flock(self._file.fileno(), mode | fcntl.LOCK_NB)


class WorkspaceLockManager:
    """为同一项目提供工作区、写事务和逐文件三级锁。"""

    def __init__(self, project_root: Path, *, state_root: Path | None = None) -> None:
        self.project_root = project_root.resolve()
        self.state_root = (state_root or project_root).resolve()
        self.directory = self.state_root / ".yy" / "sandbox" / "locks"
        if self.state_root != self.project_root:
            workspace_key = hashlib.sha256(
                os.path.normcase(str(self.project_root)).encode("utf-8"),
            ).hexdigest()[:16]
            self.directory /= workspace_key
        self._workspace_path = self.directory / "workspace.lock"
        self._mutation_path = self.directory / "mutation.lock"

    @asynccontextmanager
    async def workspace_shared(self) -> AsyncIterator[None]:
        """允许普通文件操作并发，但与 Bash/回溯/基线互斥。"""
        async with self._lease(self._workspace_path, "shared"):
            yield

    @asynccontextmanager
    async def workspace_exclusive(self) -> AsyncIterator[None]:
        """锁定整个工作区，阻止所有遵循协议的文件读写。"""
        async with self._lease(self._workspace_path, "exclusive"):
            yield

    @asynccontextmanager
    async def file_shared(self, path: Path) -> AsyncIterator[None]:
        """在调用方已持有工作区共享锁时读取单个文件。"""
        async with self._lease(self._file_lock_path(path), "shared"):
            yield

    @asynccontextmanager
    async def read(self, path: Path) -> AsyncIterator[None]:
        """读取事务：工作区共享锁加目标文件共享锁。"""
        async with self.workspace_shared():
            async with self.file_shared(path):
                yield

    @asynccontextmanager
    async def write(self, path: Path) -> AsyncIterator[None]:
        """写事务：固定顺序获取工作区、提交事务和目标文件独占锁。"""
        async with self.workspace_shared():
            async with self._lease(self._mutation_path, "exclusive"):
                async with self._lease(self._file_lock_path(path), "exclusive"):
                    yield

    @asynccontextmanager
    async def _lease(self, path: Path, mode: LockMode) -> AsyncIterator[None]:
        local = _local_lock(path)
        async with local.acquire(mode):
            handle = _SystemLockHandle(path, mode)
            try:
                while not handle.try_acquire():
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                yield
            except asyncio.CancelledError:
                raise
            finally:
                if handle.locked:
                    handle.release()
                else:
                    handle.close_unlocked()

    def _file_lock_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if self.project_root != resolved and self.project_root not in resolved.parents:
            raise PermissionError("文件锁路径必须位于项目工作区内")
        normalized = os.path.normcase(str(resolved))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.directory / "files" / f"{digest}.lock"


def _local_lock(path: Path) -> _FairAsyncRWLock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = _FairAsyncRWLock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _windows_lock_api():
    """延迟构造 Windows LockFileEx 声明，非 Windows 平台不会导入该 API。"""
    global _WINDOWS_API
    if _WINDOWS_API is not None:
        return _WINDOWS_API
    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    _WINDOWS_API = (kernel32, Overlapped)
    return _WINDOWS_API
