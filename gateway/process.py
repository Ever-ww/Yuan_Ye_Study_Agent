"""Gateway 单实例发现、后台启动、停止和日志定位。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

import httpx

from gateway.security import GatewayCredentials


class GatewayProcessManager:
    def __init__(self, agent_root: Path, port: int = 8765) -> None:
        self.agent_root = agent_root.resolve()
        self.port = port
        self.directory = self.agent_root / ".yy" / "gateway"
        self.instance_path = self.directory / "instance.json"
        self.lock_path = self.directory / "instance.lock"
        self.startup_lock_path = self.directory / "startup.lock"
        self.log_path = self.directory / "gateway.log"
        self.stop_request_path = self.directory / "stop.request"
        self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def status(self) -> dict[str, object]:
        healthy = self._healthy()
        return self._status_payload(healthy)

    def _status_payload(self, healthy: bool) -> dict[str, object]:
        """根据一次健康探测构造状态，避免紧接着重复建立 HTTP 连接。"""
        metadata = self._metadata()
        # 一次健康请求超时不等于进程已经消失。只有正式实例锁无人持有时，
        # 元数据才可判定为陈旧；否则必须保留 PID 供 stop 回收失联实例。
        if not healthy and metadata and not self._instance_lock_held():
            self.instance_path.unlink(missing_ok=True)
            metadata = {}
        return {
            "running": healthy,
            "pid": metadata.get("pid") if metadata else None,
            "port": self.port,
            "base_url": self.base_url,
            "log_path": str(self.log_path),
        }

    def ensure_running(self, timeout_seconds: float = 15.0) -> dict[str, object]:
        if self._healthy():
            return self._status_payload(True)
        deadline = time.monotonic() + timeout_seconds
        startup_lock = InstanceLock(self.startup_lock_path, timeout_seconds=timeout_seconds)
        try:
            startup_lock.acquire()
        except RuntimeError as exc:
            if self._healthy():
                return self._status_payload(True)
            raise RuntimeError("等待 Gateway 启动协调锁超时") from exc
        try:
            # 拿到跨进程启动锁后必须重新探测，避免前一个客户端刚刚完成启动。
            if self._healthy():
                return self._status_payload(True)
            self._remove_stale_metadata()
            if self._instance_lock_held():
                owner = self._instance_owner_pid()
                suffix = f" PID={owner}" if owner is not None else ""
                while time.monotonic() < deadline:
                    if self._healthy():
                        return self._status_payload(True)
                    time.sleep(0.15)
                raise RuntimeError(
                    f"已有 Gateway 实例{suffix}持有状态锁，但健康接口不可用；"
                    "请先执行 gateway stop 后重试",
                )
            if not _port_available(self.port):
                if self._healthy():
                    return self._status_payload(True)
                raise RuntimeError(f"端口 {self.port} 已被其他程序占用，Gateway 无法启动")
            self._rotate_logs()
            command = _gateway_command(self.agent_root, self.port)
            creationflags = 0
            start_new_session = os.name != "nt"
            if os.name == "nt":
                creationflags = _windows_background_creationflags()
            child_environment = os.environ.copy()
            child_environment["PYTHONUTF8"] = "1"
            child_environment["PYTHONIOENCODING"] = "utf-8"
            with self.log_path.open("a", encoding="utf-8") as log:
                subprocess.Popen(
                    command,
                    cwd=self.agent_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    close_fds=True,
                    start_new_session=start_new_session,
                    creationflags=creationflags,
                    env=child_environment,
                )
            return self._wait_until_healthy(deadline)
        finally:
            startup_lock.close()

    def _wait_until_healthy(self, deadline: float) -> dict[str, object]:
        while time.monotonic() < deadline:
            if self._healthy():
                return self._status_payload(True)
            time.sleep(0.15)
        raise RuntimeError(f"Gateway 启动超时；请查看日志：{self.log_path}")

    def stop(self, timeout_seconds: float = 10.0) -> bool:
        startup_lock = InstanceLock(self.startup_lock_path, timeout_seconds=timeout_seconds)
        startup_lock.acquire()
        try:
            pid = self._instance_owner_pid()
            locked = self._instance_lock_held()
            if not locked:
                self._remove_stale_metadata()
                return False
            if pid is None:
                raise RuntimeError(
                    "检测到失联 Gateway 持有状态锁，但旧锁没有 PID；"
                    "请结束对应的 gateway run-internal 进程后重试",
                )
            try:
                self.stop_request_path.write_text(str(pid), encoding="utf-8")
            except OSError:
                pass
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if not _pid_alive(pid) or not self._instance_lock_held():
                    self._cleanup_stopped_instance()
                    return True
                time.sleep(0.1)
            # 健康接口已经失效时，优雅停止请求可能无人消费；仅终止由
            # instance.json/instance.lock 明确记录的 Gateway PID。
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            forced_deadline = time.monotonic() + 2.0
            while time.monotonic() < forced_deadline:
                if not _pid_alive(pid) or not self._instance_lock_held():
                    self._cleanup_stopped_instance()
                    return True
                time.sleep(0.1)
            raise RuntimeError(f"Gateway 未在 {timeout_seconds + 2:g} 秒内停止")
        finally:
            startup_lock.close()

    def _cleanup_stopped_instance(self) -> None:
        self.instance_path.unlink(missing_ok=True)
        self.stop_request_path.unlink(missing_ok=True)

    def token(self) -> str:
        return GatewayCredentials(self.directory).load_or_create()

    def _healthy(self) -> bool:
        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/health",
                timeout=0.5,
                trust_env=False,
            )
            payload = response.json()
            return (
                response.status_code == 200
                and payload.get("status") == "ok"
                and payload.get("service") == "yuan-ye-agent-gateway"
            )
        except (httpx.HTTPError, ValueError):
            return False

    def _metadata(self) -> dict[str, object]:
        if not self.instance_path.exists():
            return {}
        try:
            value = json.loads(self.instance_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _remove_stale_metadata(self) -> None:
        if not self._healthy() and not self._instance_lock_held():
            self.instance_path.unlink(missing_ok=True)

    def _instance_lock_held(self) -> bool:
        probe = InstanceLock(self.lock_path)
        try:
            probe.acquire()
        except RuntimeError:
            return True
        else:
            probe.close()
            return False

    def _instance_owner_pid(self) -> int | None:
        metadata = self._metadata()
        value = metadata.get("pid") if metadata else None
        if isinstance(value, int) and value > 0:
            return value
        try:
            raw = self.lock_path.read_bytes().replace(b"\0", b"").strip()
            owner = int(raw) if raw else 0
            return owner if owner > 0 else None
        except (OSError, ValueError):
            return None

    def _rotate_logs(self, max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> None:
        """启动前轮转 Gateway 日志，防止后台进程长期运行耗尽磁盘。"""
        try:
            if not self.log_path.exists() or self.log_path.stat().st_size < max_bytes:
                return
            oldest = self.log_path.with_name(f"{self.log_path.name}.{backups}")
            oldest.unlink(missing_ok=True)
            for index in range(backups - 1, 0, -1):
                source = self.log_path.with_name(f"{self.log_path.name}.{index}")
                if source.exists():
                    source.replace(self.log_path.with_name(f"{self.log_path.name}.{index + 1}"))
            self.log_path.replace(self.log_path.with_name(f"{self.log_path.name}.1"))
        except OSError:
            # 日志轮转失败不能阻止 Gateway 启动；新日志仍尝试追加到原路径。
            return


class InstanceLock:
    """持有进程级锁，阻止第二个 Gateway 写入同一个 Agent Home。"""

    def __init__(self, path: Path, *, timeout_seconds: float = 0.0) -> None:
        self.path = path
        self.timeout_seconds = max(0.0, timeout_seconds)
        self.handle: IO[bytes] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        handle = self.path.open("r+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RuntimeError("已有 Gateway 实例持有状态锁") from exc
                time.sleep(0.05)
        handle.seek(0)
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.truncate()
        handle.flush()
        self.handle = handle

    def close(self) -> None:
        handle = self.handle
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self.handle = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def run_gateway(agent_root: Path, port: int) -> None:
    import uvicorn
    from Agent import load_runtime_config
    from gateway.api import create_gateway_api
    from gateway.models import now_iso

    root = agent_root.resolve()
    manager = GatewayProcessManager(root, port)
    instance_lock = InstanceLock(manager.lock_path)
    try:
        instance_lock.acquire()
    except RuntimeError:
        # 兼容旧客户端竞争产生的重复子进程：正式实例锁已被持有就说明
        # 胜出进程正在启动或运行，本进程直接安静退出，不污染后台日志。
        return
    try:
        if not _port_available(port):
            if manager._healthy():
                return
            raise RuntimeError(f"端口 {port} 已被占用")
        manager.stop_request_path.unlink(missing_ok=True)
        token = manager.token()
        metadata = {
            "pid": os.getpid(),
            "port": port,
            "started_at": now_iso(),
            "version": 1,
        }
        temporary = manager.instance_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(manager.instance_path)
        try:
            config = load_runtime_config(root, gateway_port=port)
            server = uvicorn.Server(uvicorn.Config(
                create_gateway_api(access_token=token),
                host="127.0.0.1",
                port=port,
                log_config=None,
            ))

            async def serve_until_stopped() -> None:
                loop = asyncio.get_running_loop()
                previous_handler = loop.get_exception_handler()
                protocol_filter = _GatewayProtocolNoiseFilter()
                uvicorn_logger = logging.getLogger("uvicorn.error")
                uvicorn_logger.addFilter(protocol_filter)

                def handle_loop_exception(current_loop, context) -> None:
                    if _is_benign_closed_h11_response(context):
                        return
                    if previous_handler is not None:
                        previous_handler(current_loop, context)
                    else:
                        current_loop.default_exception_handler(context)

                loop.set_exception_handler(handle_loop_exception)
                serving = asyncio.create_task(server.serve())
                try:
                    while not serving.done():
                        if manager.stop_request_path.exists():
                            server.should_exit = True
                            break
                        await asyncio.sleep(0.2)
                    await serving
                finally:
                    try:
                        if not serving.done():
                            server.should_exit = True
                            await serving
                    finally:
                        loop.set_exception_handler(previous_handler)
                        uvicorn_logger.removeFilter(protocol_filter)

            asyncio.run(serve_until_stopped())
        finally:
            manager.instance_path.unlink(missing_ok=True)
            manager.stop_request_path.unlink(missing_ok=True)
    finally:
        instance_lock.close()


class _GatewayProtocolNoiseFilter(logging.Filter):
    """过滤 Windows Proactor 在已关闭连接上重复报告的无效 HTTP 警告。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != "Invalid HTTP request received."


def _is_benign_closed_h11_response(context: dict[str, object]) -> bool:
    exception = context.get("exception")
    if exception is None:
        return False
    return (
        type(exception).__name__ == "LocalProtocolError"
        and type(exception).__module__.startswith("h11")
        and "can't handle event type Response" in str(exception)
        and "state=CLOSED" in str(exception)
    )


def _gateway_command(agent_root: Path, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "gateway",
            "run-internal",
            "--agent-root",
            str(agent_root),
            "--port",
            str(port),
        ]
    return [
        sys.executable,
        "-m",
        "gateway",
        "run-internal",
        "--agent-root",
        str(agent_root),
        "--port",
        str(port),
    ]


def _windows_background_creationflags() -> int:
    """创建无控制台后台进程；DETACHED_PROCESS 会使 CREATE_NO_WINDOW 失效。"""
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        try:
            handle.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True
