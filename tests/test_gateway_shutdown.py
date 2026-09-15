"""Idle ASGI sockets must not retain an already-drained Gateway process."""
import asyncio
from contextlib import aclosing
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from Agent import load_runtime_config
from gateway.api import _subscription_events
from gateway.application import GatewayApplication
from backup import AgentHomeMaintenanceCoordinator, AgentHomeWriteGate
from backup.lifecycle_store import LifecycleStore
from gateway.process import (
    GatewayProcessManager,
    resume_completed_operator_stop,
    resume_completed_operator_stop_before_bootstrap,
)


class WebSocketShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_disconnect_finishes_without_waiting_for_event(self):
        incoming = asyncio.Queue()
        socket = SimpleNamespace(receive=incoming.get)
        stream = _subscription_events(socket, asyncio.Queue())
        next_event = asyncio.create_task(anext(stream))
        await incoming.put({"type": "websocket.disconnect"})
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(next_event, 1)

    async def test_delivery_and_cancellation_reap_helpers(self):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        before = asyncio.all_tasks()
        async with aclosing(_subscription_events(SimpleNamespace(receive=incoming.get), outgoing)) as stream:
            await incoming.put({"type": "websocket.receive", "text": "ignored"})
            await outgoing.put("event")
            self.assertEqual(await anext(stream), "event")
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertFalse(asyncio.all_tasks() - before)

    async def test_only_exact_clean_operator_stop_may_auto_resume(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            app = GatewayApplication(load_runtime_config(root))
            manager = GatewayProcessManager(root)
            try:
                await app.quiesce(5, "operator stop")
                snapshot = app.maintenance.snapshot
                marker = {key: getattr(snapshot, key) for key in ("maintenance_epoch", "revision", "operation_id")}
                # No teardown evidence: still control-only.
                self.assertFalse(await resume_completed_operator_stop(manager, app))
                manager.stopped_path.write_text(json.dumps({**marker, "revision": -1}))
                self.assertFalse(await resume_completed_operator_stop(manager, app))
                manager.stopped_path.write_text(json.dumps(marker))
                self.assertTrue(await resume_completed_operator_stop(manager, app))
                self.assertEqual(app.write_gate.state.value, "running")
                self.assertFalse(manager.stopped_path.exists())
                await app.quiesce(5, "backup")
                current = app.maintenance.snapshot
                manager.stopped_path.write_text(json.dumps({key: getattr(current, key) for key in marker}))
                self.assertFalse(await resume_completed_operator_stop(manager, app))
            finally:
                await app.close()


class StopAcknowledgementTests(unittest.TestCase):
    def test_ensure_running_reports_control_only_maintenance_without_timeout(self):
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value))
            health = {
                "status": "ok",
                "service": "yuan-ye-agent-gateway",
                "accepting_work": False,
                "maintenance": {
                    "state": "failed",
                    "maintenance_epoch": 7,
                    "revision": 11,
                },
            }
            with patch.object(manager, "_health_payload", return_value=health):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"failed.*python run.py gateway resume --epoch 7 --revision 11",
                ):
                    manager.ensure_running(timeout_seconds=30)

    def test_clean_stop_resumes_before_mutable_gateway_bootstrap(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            gate = AgentHomeWriteGate()
            lifecycle = AgentHomeMaintenanceCoordinator(root, gate)
            asyncio.run(lifecycle.freeze("operator stop", timeout_seconds=5))
            snapshot = lifecycle.snapshot
            lifecycle.close()
            manager = GatewayProcessManager(root)
            manager.stopped_path.write_text(json.dumps({
                key: getattr(snapshot, key)
                for key in ("maintenance_epoch", "revision", "operation_id")
            }), encoding="utf-8")

            self.assertTrue(resume_completed_operator_stop_before_bootstrap(manager))
            self.assertEqual(LifecycleStore(root).read().state.value, "running")
            self.assertFalse(manager.stopped_path.exists())

    def test_drain_failure_is_reported_without_waiting_entire_deadline(self):
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value))
            def read_ack(path):
                return {"request_hash": hashlib.sha256(manager.stop_request_path.read_bytes()).hexdigest(),
                        "instance_id": "owner", "status": "failed", "state": "failed", "error_type": "TimeoutError"}
            with patch.object(manager, "_instance_owner_pid", return_value=123), \
                 patch.object(manager, "_instance_lock_held", return_value=True), \
                 patch.object(manager, "_metadata", return_value={"instance_id": "owner"}), \
                 patch("gateway.process._pid_alive", return_value=True), \
                 patch("gateway.process._read_control_json", side_effect=read_ack):
                with self.assertRaisesRegex(RuntimeError, "drain 失败.*TimeoutError"):
                    manager.stop(timeout_seconds=1)

    def test_successful_drain_has_separate_shutdown_grace(self):
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value))
            with patch.object(manager, "_instance_owner_pid", return_value=123), \
                 patch.object(manager, "_instance_lock_held", side_effect=[True, False]), \
                 patch.object(manager, "_metadata", return_value={"instance_id": "owner"}), \
                 patch("gateway.process._pid_alive", side_effect=[True, False]), \
                 patch("gateway.process.time.monotonic", side_effect=[0, 0, .5, 1.5]), \
                 patch("gateway.process.time.sleep"):
                self.assertTrue(manager.stop(timeout_seconds=1, shutdown_grace_seconds=2))
