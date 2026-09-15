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
import hashlib
from datetime import datetime
from uuid import uuid4
from pathlib import Path
from typing import IO

import httpx

from gateway.security import GatewayCredentials
from backup import (
    AgentHomeMaintenanceCoordinator,
    AgentHomeWriteGate,
    SensitiveEnvSanitizer,
    assert_restore_inactive,
    external_control_root,
)
from backup.control import _atomic_json
from backup.models import GatewayControlRequest, MaintenanceState
from backup.lifecycle_store import LifecycleStore


class GatewayProcessManager:
    def __init__(self, agent_root: Path, port: int = 8765) -> None:
        self.agent_root = agent_root.resolve()
        self.port = port
        self.directory = external_control_root(self.agent_root) / "control" / "gateway"
        self.instance_path = self.directory / "instance.json"
        self.lock_path = self.directory / "instance.lock"
        self.startup_lock_path = self.directory / "startup.lock"
        self.log_path = self.directory / "gateway.log"
        self.stop_request_path = self.directory / "stop.request"
        self.stop_ack_path = self.directory / "stop.ack"
        self.stopped_path = self.directory / "stopped.json"
        self.restart_request_path = self.directory / "restart.request"
        # This is the external control plane, never the replaceable `.yy` tree.
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
            "maintenance": (LifecycleStore(self.agent_root).read().model_dump(mode="json")
                            if (self.directory / "lifecycle.sqlite3").exists() else None),
        }

    def ensure_running(self, timeout_seconds: float = 45.0) -> dict[str, object]:
        """Return a healthy Gateway, allowing bounded time for cold recovery.

        A production Agent Home may need to verify a sizeable SQLite store,
        reconcile durable work and build a Runtime resource Generation before
        ASGI becomes ready.  Fifteen seconds caused the CLI to report failure
        while that healthy child was still completing startup.
        """
        assert_restore_inactive(self.agent_root)
        health = self._health_payload()
        if self._accepts_work(health):
            return self._status_payload(True)
        self._raise_if_control_only(health)
        deadline = time.monotonic() + timeout_seconds
        startup_lock = InstanceLock(self.startup_lock_path, timeout_seconds=timeout_seconds)
        try:
            startup_lock.acquire()
        except RuntimeError as exc:
            health = self._health_payload()
            if self._accepts_work(health):
                return self._status_payload(True)
            self._raise_if_control_only(health)
            raise RuntimeError("等待 Gateway 启动协调锁超时") from exc
        try:
            # 拿到跨进程启动锁后必须重新探测，避免前一个客户端刚刚完成启动。
            assert_restore_inactive(self.agent_root)
            health = self._health_payload()
            if self._accepts_work(health):
                return self._status_payload(True)
            self._raise_if_control_only(health)
            self._remove_stale_metadata()
            if self._instance_lock_held():
                owner = self._instance_owner_pid()
                suffix = f" PID={owner}" if owner is not None else ""
                while time.monotonic() < deadline:
                    health = self._health_payload()
                    if self._accepts_work(health):
                        return self._status_payload(True)
                    self._raise_if_control_only(health)
                    time.sleep(0.15)
                raise RuntimeError(
                    f"已有 Gateway 实例{suffix}持有状态锁，但健康接口不可用；"
                    "请先执行 gateway stop 后重试",
                )
            if not _port_available(self.port):
                health = self._health_payload()
                if self._accepts_work(health):
                    return self._status_payload(True)
                self._raise_if_control_only(health)
                raise RuntimeError(f"端口 {self.port} 已被其他程序占用，Gateway 无法启动")
            self._rotate_logs()
            command = _gateway_command(self.agent_root, self.port)
            creationflags = 0
            start_new_session = os.name != "nt"
            if os.name == "nt":
                creationflags = _windows_background_creationflags()
            child_environment = SensitiveEnvSanitizer.subprocess_env({
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }, allowed_names={"YY_BACKUP_PASSPHRASE"},
               trusted_sensitive_names={"YY_BACKUP_PASSPHRASE"})
            self.directory.mkdir(parents=True, exist_ok=True)
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
            health = self._health_payload()
            if self._accepts_work(health):
                return self._status_payload(True)
            self._raise_if_control_only(health)
            time.sleep(0.15)
        raise RuntimeError(f"Gateway 启动超时；请查看日志：{self.log_path}")

    def stop(self, timeout_seconds: float = 30.0, *, shutdown_grace_seconds: float = 10.0) -> bool:
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
            metadata = self._metadata()
            if not metadata.get("instance_id"):
                raise RuntimeError("Legacy Gateway has no control identity; stop it explicitly before upgrading")
            request = GatewayControlRequest(
                request_id=uuid4().hex, instance_id=str(metadata["instance_id"]), action="stop",
                reason="operator stop", requested_at=datetime.now().astimezone(),
                requested_by="gateway-process-manager", timeout_seconds=timeout_seconds,
            )
            _atomic_json(self.stop_request_path, request.model_dump_json())
            # Drain and ASGI/socket teardown are separate phases. Giving both
            # the same deadline falsely reports failure at the drain boundary.
            deadline = time.monotonic() + timeout_seconds + shutdown_grace_seconds
            request_hash = hashlib.sha256(self.stop_request_path.read_bytes()).hexdigest()
            ack = {}
            while time.monotonic() < deadline:
                # Lock release precedes Python's final process/stdio teardown.
                # Wait for both, otherwise Windows still holds gateway.log open.
                if not _pid_alive(pid) and not self._instance_lock_held():
                    self._cleanup_stopped_instance()
                    return True
                ack = _read_control_json(self.stop_ack_path)
                if ack.get("request_hash") == request_hash and ack.get("instance_id") == request.instance_id:
                    if ack.get("status") == "failed":
                        raise RuntimeError(
                            f"Gateway drain 失败（{ack.get('error_type')}）；未强杀任务。"
                            f"状态={ack.get('state')}。运行 gateway status 查看详情；"
                            "工作结束后重试 stop，或按 epoch/revision 执行 gateway resume。"
                        )
                time.sleep(0.1)
            phase = "已 quiesced，但连接/进程退出超时" if (
                ack.get("request_hash") == request_hash and ack.get("status") == "completed"
            ) else f"drain 超过 {timeout_seconds:g} 秒"
            raise RuntimeError(f"Gateway {phase}；未强杀任务，请检查 gateway status、stop.ack 与 maintenance 状态")
        finally:
            startup_lock.close()

    def _cleanup_stopped_instance(self) -> None:
        self.instance_path.unlink(missing_ok=True)
        self.stop_request_path.unlink(missing_ok=True)

    def token(self) -> str:
        assert_restore_inactive(self.agent_root)
        return GatewayCredentials(self.directory).load_or_create()

    def _healthy(self, *, require_accepting_work: bool = False) -> bool:
        """Return process health, optionally requiring normal work admission.

        The ASGI server deliberately answers health probes while durable
        maintenance recovery is still quiesced or resuming.  That proves the
        process is alive, but it is not sufficient for an interactive client:
        mutation endpoints correctly return 503 until ``accepting_work`` is
        true.  Callers that are about to submit work must request readiness.
        """
        payload = self._health_payload()
        return self._accepts_work(payload) if require_accepting_work else payload is not None

    def _health_payload(self) -> dict[str, object] | None:
        """Return a validated local health payload without conflating readiness."""
        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/health",
                timeout=0.5,
                trust_env=False,
            )
            payload = response.json()
            if (
                response.status_code == 200
                and payload.get("status") == "ok"
                and payload.get("service") == "yuan-ye-agent-gateway"
            ):
                return dict(payload)
            return None
        except (httpx.HTTPError, ValueError):
            return None

    @staticmethod
    def _accepts_work(payload: dict[str, object] | None) -> bool:
        return payload is not None and payload.get("accepting_work") is True

    @staticmethod
    def _raise_if_control_only(payload: dict[str, object] | None) -> None:
        """Fail fast when the process is healthy but maintenance blocks work.

        Waiting for the cold-start deadline cannot change a durable maintenance
        decision.  Surface the exact recovery coordinates instead of reporting
        the healthy control-plane process as a startup timeout.
        """
        if payload is None or payload.get("accepting_work") is True:
            return
        maintenance = payload.get("maintenance")
        snapshot = maintenance if isinstance(maintenance, dict) else {}
        state = str(snapshot.get("state") or "maintenance")
        epoch = snapshot.get("maintenance_epoch")
        revision = snapshot.get("revision")
        recovery = ""
        if isinstance(epoch, int) and isinstance(revision, int):
            recovery = (
                f"；确认状态后执行 python run.py gateway resume --epoch {epoch} "
                f"--revision {revision}"
            )
        raise RuntimeError(
            f"Gateway 控制接口在线，但当前处于 {state}，不接收 Agent 工作{recovery}",
        )

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
    from gateway.application import GatewayApplication
    from gateway.models import now_iso

    root = agent_root.resolve()
    assert_restore_inactive(root)
    manager = GatewayProcessManager(root, port)
    instance_lock = InstanceLock(manager.lock_path)
    try:
        instance_lock.acquire()
    except RuntimeError:
        # 兼容旧客户端竞争产生的重复子进程：正式实例锁已被持有就说明
        # 胜出进程正在启动或运行，本进程直接安静退出，不污染后台日志。
        return
    try:
        # Restore may publish its Fence while this process was waiting for the
        # instance lock. Re-check after acquisition and before any `.yy` write.
        assert_restore_inactive(root)
        if not _port_available(port):
            if manager._healthy():
                return
            raise RuntimeError(f"端口 {port} 已被占用")
        token = manager.token()
        instance_id = uuid4().hex
        metadata = {
            "pid": os.getpid(),
            "port": port,
            "started_at": now_iso(),
            "version": 1,
            "instance_id": instance_id,
        }
        temporary = manager.instance_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(manager.instance_path)
        try:
            config = load_runtime_config(root, gateway_port=port)
            # A clean operator stop deliberately leaves the durable lifecycle
            # QUIESCED. Resume that verified state before GatewayApplication
            # constructs components which may need to publish a new Runtime
            # resource Generation. Otherwise the bootstrap write is correctly
            # denied by WriteGate and the control API can never come up to resume.
            resume_completed_operator_stop_before_bootstrap(manager)
            api = create_gateway_api(GatewayApplication(config), access_token=token)
            server = uvicorn.Server(uvicorn.Config(
                api,
                host="127.0.0.1",
                port=port,
                log_config=None,
                # User work has already crossed the durable quiesce boundary
                # for a stop request. Bound leftover HTTP/WebSocket teardown.
                timeout_graceful_shutdown=5,
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
                await resume_completed_operator_stop(manager, api.state.gateway)
                serving = asyncio.create_task(server.serve())
                stopped_by_request = False
                try:
                    while not serving.done():
                        if not server.started:
                            # Lifespan bootstrap must settle before the stop
                            # participant set can truthfully acknowledge drain.
                            await asyncio.sleep(0.2)
                            continue
                        stop_ready = False
                        if manager.stop_request_path.exists():
                            try:
                                stop_ready = await process_control_request(manager, api.state.gateway, instance_id)
                            except Exception as exc:
                                # Control-file I/O failure is not permission to
                                # shut down/cancel active user work.
                                logging.getLogger(__name__).error("Gateway control request failed: %s", type(exc).__name__)
                        if stop_ready:
                            stopped_by_request = True
                            server.should_exit = True
                            break
                        if manager.restart_request_path.exists():
                            server.should_exit = True
                            break
                        await asyncio.sleep(0.2)
                    await serving
                    if stopped_by_request:
                        snapshot = api.state.gateway.maintenance.snapshot
                        if snapshot.state == MaintenanceState.QUIESCED and snapshot.reason == "operator stop":
                            _atomic_json(manager.stopped_path, json.dumps({
                                "instance_id": instance_id,
                                "maintenance_epoch": snapshot.maintenance_epoch,
                                "revision": snapshot.revision,
                                "operation_id": snapshot.operation_id,
                            }, sort_keys=True))
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
    finally:
        instance_lock.close()


async def process_control_request(manager, gateway, instance_id: str) -> bool:
    """Request and ack are delivery evidence; only lifecycle decides readiness."""
    try:
        raw = manager.stop_request_path.read_bytes()
    except FileNotFoundError:
        return False
    digest = hashlib.sha256(raw).hexdigest()
    if manager.stop_ack_path.exists():
        try:
            ack = json.loads(manager.stop_ack_path.read_text(encoding="utf-8"))
            if not isinstance(ack, dict):
                ack = {}
        except (OSError, ValueError):
            ack = {}  # Rebuild delivery result from the request and canonical state.
        if ack.get("request_hash") == digest and ack.get("instance_id") == instance_id:
            return bool(ack.get("status") == "completed" and ack.get("action") == "stop"
                        and ack.get("instance_id") == instance_id
                        and gateway.write_gate.state == MaintenanceState.QUIESCED)
    result = {"version": 1, "request_hash": digest, "instance_id": instance_id,
              "completed_at": datetime.now().astimezone().isoformat()}
    try:
        request = GatewayControlRequest.model_validate_json(raw)
        result.update(request_id=request.request_id, action=request.action)
        if request.instance_id != instance_id:
            raise ValueError("Control request targets a different Gateway instance")
        if gateway.write_gate.state != MaintenanceState.QUIESCED:
            await gateway.quiesce(request.timeout_seconds, request.reason)
        result.update(status="completed", lifecycle_revision=gateway.maintenance.snapshot.revision,
                      state=gateway.write_gate.state.value)
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__,
                      state=gateway.write_gate.state.value,
                      lifecycle_revision=gateway.maintenance.snapshot.revision)
    result["completed_at"] = datetime.now().astimezone().isoformat()
    _atomic_json(manager.stop_ack_path, json.dumps(result, sort_keys=True))
    return result.get("status") == "completed" and result.get("action") == "stop"


def _read_control_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


async def resume_completed_operator_stop(manager, gateway) -> bool:
    """A clean operator stop can restart; crash/backup/FAILED stays control-only.

    The marker is only evidence of completed ASGI teardown, not lifecycle
    authority. Resume still validates the canonical epoch/revision, restore
    fence, participants and database health through the maintenance controller.
    """
    marker = _read_control_json(manager.stopped_path)
    snapshot = gateway.maintenance.snapshot
    if not marker or snapshot.state != MaintenanceState.QUIESCED or snapshot.reason != "operator stop":
        return False
    if any(marker.get(key) != getattr(snapshot, key) for key in ("maintenance_epoch", "revision", "operation_id")):
        return False
    await gateway.resume(snapshot.maintenance_epoch, expected_revision=snapshot.revision)
    manager.stopped_path.unlink(missing_ok=True)
    return True


def resume_completed_operator_stop_before_bootstrap(
    manager: GatewayProcessManager,
) -> bool:
    """Resume an exactly proven clean stop before mutable app construction.

    This is intentionally narrower than general maintenance recovery. Backup,
    restore, failed or interrupted maintenance states remain control-only and
    require the normal explicit recovery path.
    """
    marker = _read_control_json(manager.stopped_path)
    store = LifecycleStore(manager.agent_root)
    snapshot = store.read()
    if (
        not marker
        or snapshot.state != MaintenanceState.QUIESCED
        or snapshot.reason != "operator stop"
        or any(
            marker.get(key) != getattr(snapshot, key)
            for key in ("maintenance_epoch", "revision", "operation_id")
        )
    ):
        return False
    assert_restore_inactive(manager.agent_root)
    gate = AgentHomeWriteGate()
    coordinator = AgentHomeMaintenanceCoordinator(manager.agent_root, gate)
    try:
        asyncio.run(coordinator.resume(
            snapshot.maintenance_epoch, expected_revision=snapshot.revision,
        ))
    finally:
        coordinator.close()
    manager.stopped_path.unlink(missing_ok=True)
    return True


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
