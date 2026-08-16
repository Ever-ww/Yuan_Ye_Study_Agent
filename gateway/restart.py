"""Durable, safety-boundary-aware Gateway restart coordination."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from backup import AgentHomeMaintenanceCoordinator, SensitiveEnvSanitizer, external_control_root
from gateway.process import GatewayProcessManager, _pid_alive, _windows_background_creationflags

if TYPE_CHECKING:
    from gateway.state_controller import StateController


class GatewayRestartCoordinator:
    """Persist one restart intent and stop only after participants are quiescent."""

    def __init__(
        self, *, agent_root: Path, source_root: Path, port: int,
        gateway_epoch: str, state_controller: "StateController",
        maintenance: AgentHomeMaintenanceCoordinator, is_idle: Callable[[], bool],
        timeout_seconds: float,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.source_root = source_root.resolve()
        self.port = port
        self.gateway_epoch = gateway_epoch
        self.state_controller = state_controller
        self.maintenance = maintenance
        self.is_idle = is_idle
        self.timeout_seconds = timeout_seconds
        self.manager = GatewayProcessManager(self.agent_root, port)
        self._tasks: set[asyncio.Task[None]] = set()

    async def request(self, *, stable_key: str, expected_commit: str, run_id: str) -> str:
        import hashlib

        request_id = hashlib.sha256(f"restart:{stable_key}".encode("utf-8")).hexdigest()[:32]
        payload = {
            "request_id": request_id, "stable_key": stable_key, "run_id": run_id,
            "expected_commit": expected_commit, "source_root": str(self.source_root),
            "agent_root": str(self.agent_root), "port": self.port,
        }
        row = self.state_controller.create_gateway_restart_request(
            request_id, stable_key, expected_pid=os.getpid(),
            expected_gateway_epoch=self.gateway_epoch, expected_commit=expected_commit,
            request=payload,
        )
        if str(row["status"]) in {"pending", "waiting"} and not any(
            not task.done() and task.get_name() == f"gateway-restart:{request_id}"
            for task in self._tasks
        ):
            task = asyncio.create_task(
                self._wait_and_restart(request_id, stable_key, payload),
                name=f"gateway-restart:{request_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return request_id

    async def recover_pending(self) -> int:
        recovered = 0
        for row in self.state_controller.pending_gateway_restart_requests():
            try:
                payload = json.loads(str(row["request_json"]))
            except (json.JSONDecodeError, TypeError):
                self.state_controller.update_gateway_restart_request(
                    str(row["request_id"]), "failed",
                )
                continue
            await self.request(
                stable_key=str(row["stable_key"]),
                expected_commit=str(row["expected_commit"]),
                run_id=str(payload.get("run_id") or ""),
            )
            recovered += 1
        return recovered

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _wait_and_restart(
        self, request_id: str, stable_key: str, payload: dict[str, Any],
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        self.state_controller.update_gateway_restart_request(request_id, "waiting")
        # Let the Dream scheduler callback release its tick lock before quiescing it.
        await asyncio.sleep(0)
        while not self.is_idle():
            if asyncio.get_running_loop().time() >= deadline:
                self.state_controller.mark_harness_dream_restart_timeout(stable_key, request_id)
                return
            await asyncio.sleep(0.25)
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        self.state_controller.update_gateway_restart_request(request_id, "requested")
        try:
            await self.maintenance.freeze(
                f"Harness Dream restart {request_id}", timeout_seconds=remaining,
            )
        except Exception:
            self.state_controller.mark_harness_dream_restart_timeout(stable_key, request_id)
            return
        helper = _restart_helper_command(
            self.agent_root, self.source_root, self.port, os.getpid(), request_id,
            str(payload["expected_commit"]), self.timeout_seconds,
        )
        flags = _windows_background_creationflags() if os.name == "nt" else 0
        try:
            subprocess.Popen(
                helper, cwd=self.agent_root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                start_new_session=os.name != "nt", creationflags=flags,
                env=SensitiveEnvSanitizer.subprocess_env({"PYTHONUTF8": "1"}),
            )
            temporary = self.manager.restart_request_path.with_suffix(".partial")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(self.manager.restart_request_path)
        except Exception:
            await self.maintenance.resume(self.maintenance.snapshot.maintenance_epoch)
            self.state_controller.update_gateway_restart_request(request_id, "failed")
            return


def run_restart_helper(
    agent_root: Path, source_root: Path, port: int, expected_pid: int,
    request_id: str, expected_commit: str, timeout_seconds: float,
) -> None:
    """Wait for the old fenced process, then start and verify the new Gateway."""
    manager = GatewayProcessManager(agent_root, port)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(expected_pid) and not manager._instance_lock_held():
            break
        time.sleep(0.2)
    else:
        _update_restart_row(agent_root, request_id, "restart_wait_timeout")
        return
    manager.restart_request_path.unlink(missing_ok=True)
    try:
        manager.ensure_running(timeout_seconds=20)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source_root, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=SensitiveEnvSanitizer.subprocess_env(), timeout=10,
        ).stdout.decode("utf-8", errors="replace").strip()
        if expected_commit and head != expected_commit:
            _update_restart_row(agent_root, request_id, "commit_mismatch")
            return
        _update_restart_row(agent_root, request_id, "completed")
    except Exception:
        _update_restart_row(agent_root, request_id, "failed")


def _update_restart_row(agent_root: Path, request_id: str, status: str) -> None:
    database = agent_root / ".yy" / "gateway" / "gateway.sqlite3"
    if not database.exists():
        return
    try:
        with sqlite3.connect(database, timeout=30) as connection:
            connection.execute(
                "UPDATE gateway_restart_requests SET status=?,updated_at=datetime('now') "
                "WHERE request_id=?", (status, request_id),
            )
    except sqlite3.Error:
        return


def _restart_helper_command(
    agent_root: Path, source_root: Path, port: int, expected_pid: int,
    request_id: str, expected_commit: str, timeout_seconds: float,
) -> list[str]:
    prefix = [sys.executable, "gateway"] if getattr(sys, "frozen", False) else [
        sys.executable, "-m", "gateway",
    ]
    return [
        *prefix, "restart-helper", "--agent-root", str(agent_root),
        "--source-root", str(source_root), "--port", str(port),
        "--expected-pid", str(expected_pid), "--request-id", request_id,
        "--expected-commit", expected_commit, "--timeout", str(timeout_seconds),
    ]


__all__ = ["GatewayRestartCoordinator", "run_restart_helper"]
