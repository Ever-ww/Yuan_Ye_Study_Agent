"""Gateway 单实例发现、后台启动、停止和日志定位。"""

from __future__ import annotations

import asyncio
import json
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
        self.log_path = self.directory / "gateway.log"
        self.stop_request_path = self.directory / "stop.request"
        self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def status(self) -> dict[str, object]:
        metadata = self._metadata()
        healthy = self._healthy()
        if not healthy and metadata:
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
            return self.status()
        self._remove_stale_metadata()
        if not _port_available(self.port):
            raise RuntimeError(f"端口 {self.port} 已被其他程序占用，Gateway 无法启动")
        self._rotate_logs()
        command = _gateway_command(self.agent_root, self.port)
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = _windows_background_creationflags()
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
            )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._healthy():
                return self.status()
            time.sleep(0.15)
        raise RuntimeError(f"Gateway 启动超时；请查看日志：{self.log_path}")

    def stop(self, timeout_seconds: float = 10.0) -> bool:
        metadata = self._metadata()
        if not metadata or not self._healthy():
            self._remove_stale_metadata()
            return False
        pid = int(metadata["pid"])
        try:
            self.stop_request_path.write_text(str(pid), encoding="utf-8")
        except OSError:
            pass
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self._healthy():
                self._remove_stale_metadata()
                self.stop_request_path.unlink(missing_ok=True)
                return True
            time.sleep(0.1)
        # 仅在优雅关闭超时后兜底终止，防止失联后台进程永久占用端口。
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        forced_deadline = time.monotonic() + 2.0
        while time.monotonic() < forced_deadline:
            if not self._healthy():
                self._remove_stale_metadata()
                self.stop_request_path.unlink(missing_ok=True)
                return True
            time.sleep(0.1)
        raise RuntimeError(f"Gateway 未在 {timeout_seconds + 2:g} 秒内停止")

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
        if not self._healthy():
            self.instance_path.unlink(missing_ok=True)

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

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: IO[bytes] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("已有 Gateway 实例持有状态锁") from exc
        self.handle = handle

    def close(self) -> None:
        handle = self.handle
        if handle is None:
            return
        try:
            if os.name == "nt":
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
    if not _port_available(port):
        raise RuntimeError(f"端口 {port} 已被占用")
    with InstanceLock(manager.lock_path):
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
                serving = asyncio.create_task(server.serve())
                try:
                    while not serving.done():
                        if manager.stop_request_path.exists():
                            server.should_exit = True
                            break
                        await asyncio.sleep(0.2)
                    await serving
                finally:
                    if not serving.done():
                        server.should_exit = True
                        await serving

            asyncio.run(serve_until_stopped())
        finally:
            manager.instance_path.unlink(missing_ok=True)
            manager.stop_request_path.unlink(missing_ok=True)


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


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            handle.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True
