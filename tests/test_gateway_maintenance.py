"""Maintenance is one durable authority; leases are transient liveness only."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from Agent import load_runtime_config
from backup.maintenance import AgentHomeMaintenanceCoordinator, AgentHomeWriteGate, MaintenanceBlockedError
from backup.models import MaintenanceState as S, QuiesceResult, GatewayControlRequest
from gateway.application import GatewayApplication
from gateway.api import create_gateway_api
from gateway.models import RunCreateRequest
from gateway.process import GatewayProcessManager, process_control_request
from dream import DreamScheduler, DreamStatus
from fastapi.testclient import TestClient


class Participant:
    def __init__(self, fail_resume=False):
        self.fail_resume = fail_resume
        self.paused = False

    async def quiesce(self, epoch):
        self.paused = True
        return QuiesceResult(participant="test", maintenance_epoch=epoch, acknowledged=True)

    async def resume(self, epoch):
        if self.fail_resume:
            raise RuntimeError("participant resume failed")
        self.paused = False


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.gate = AgentHomeWriteGate()
        self.lifecycle = AgentHomeMaintenanceCoordinator(self.root, self.gate)

    async def asyncTearDown(self):
        self.lifecycle.close()
        self.directory.cleanup()

    async def test_drain_rejects_new_work_but_allows_existing_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def run():
            async with self.gate.operation("runtime_pool", "run-1"):
                entered.set()
                await release.wait()
                async with self.gate.operation("tool", "tool-1", kind="tool", continuation=True):
                    self.gate.check_mutation_admission()
        task = asyncio.create_task(run())
        await entered.wait()
        draining = asyncio.create_task(self.lifecycle.quiesce(timeout=2))
        while self.gate.state == S.RUNNING:
            await asyncio.sleep(0)
        self.assertEqual(self.lifecycle.snapshot.active_runs, 1)
        for kind in ("cron", "dream", "subagent", "harness", "new_run"):
            with self.assertRaises(MaintenanceBlockedError):
                async with self.gate.operation(kind, "new"):
                    self.fail("not admitted")
        release.set()
        await task
        result = await draining
        self.assertEqual(result.state, S.QUIESCED)
        self.assertEqual(result.active_work, ())
        await self.lifecycle.resume(result.maintenance_epoch, expected_revision=result.revision)
        self.assertEqual(self.gate.state, S.RUNNING)
        self.assertEqual([r["to_state"] for r in self.lifecycle.store.history()],
                         ["quiescing", "quiesced", "resuming", "running"])

    async def test_timeout_and_cancellation_never_cancel_a_run_or_auto_resume(self):
        release, entered = asyncio.Event(), asyncio.Event()
        async def run():
            async with self.gate.operation("runtime_pool", "slow"):
                entered.set()
                await release.wait()
                self.gate.check_mutation_admission()
        task = asyncio.create_task(run())
        await entered.wait()
        with self.assertRaises(asyncio.TimeoutError):
            await self.lifecycle.quiesce(timeout=0.03)
        self.assertFalse(task.done())
        self.assertEqual(self.gate.state, S.FAILED)
        self.assertTrue(self.lifecycle.store.read().active_work)
        with self.assertRaises(MaintenanceBlockedError):
            await self.lifecycle.resume(1)
        release.set()
        await task
        await self.lifecycle.resume(1)
        self.assertEqual(self.gate.state, S.RUNNING)

    async def test_dream_cooperatively_interrupts_before_global_lease_drain(self):
        entered = asyncio.Event()
        interrupted = asyncio.Event()
        callbacks = []
        service = SimpleNamespace(
            config=SimpleNamespace(
                dream_enabled=True,
                harness_dream_enabled=False,
                dream_schedule="0 3 * * *",
                dream_timezone="UTC",
            ),
            status=lambda **_kwargs: DreamStatus(
                enabled=True,
                running=True,
                schedule="0 3 * * *",
                timezone="UTC",
                initialized_at="2026-09-01T00:00:00+00:00",
                last_completed_date=None,
            ),
        )

        async def run_day(_selected):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise

        async def on_result(result, automatic):
            callbacks.append((result, automatic))

        scheduler = DreamScheduler(
            service,
            lambda: True,
            on_result,
            clock=lambda: datetime(2026, 9, 15, 4, tzinfo=timezone.utc),
            run_day=run_day,
            write_gate=self.gate,
        )
        self.lifecycle.register("dream", scheduler)
        ticking = asyncio.create_task(scheduler.tick())
        await asyncio.wait_for(entered.wait(), 1)

        snapshot = await self.lifecycle.quiesce(timeout=1)

        self.assertEqual(snapshot.state, S.QUIESCED)
        self.assertTrue(interrupted.is_set())
        self.assertIsNone(await ticking)
        self.assertEqual(callbacks, [])
        self.assertEqual(snapshot.active_work, ())
        self.assertEqual(
            snapshot.participant_status["dream"].safe_boundary,
            "dream_interrupted_or_day_run_persisted",
        )
        await self.lifecycle.resume(
            snapshot.maintenance_epoch,
            expected_revision=snapshot.revision,
        )

    async def test_resume_health_and_participant_failure_remain_failed(self):
        participant = Participant(fail_resume=True)
        self.lifecycle.register("test", participant)
        snapshot = await self.lifecycle.quiesce()
        with self.assertRaisesRegex(RuntimeError, "participant resume"):
            await self.lifecycle.resume(snapshot.maintenance_epoch)
        self.assertEqual(self.gate.state, S.FAILED)
        participant.fail_resume = False
        self.lifecycle.health_check = AsyncMock(side_effect=RuntimeError("bad schema"))
        with self.assertRaisesRegex(RuntimeError, "bad schema"):
            await self.lifecycle.resume(snapshot.maintenance_epoch)
        self.assertEqual(self.gate.state, S.FAILED)

    async def test_illegal_transition_and_stale_revision_do_not_change_state(self):
        with self.assertRaises(MaintenanceBlockedError):
            self.gate.transition(S.RUNNING, S.RESTORING, "illegal")
        with self.assertRaises(MaintenanceBlockedError):
            await self.lifecycle.resume(0)
        self.assertEqual(self.gate.state, S.RUNNING)
        snapshot = await self.lifecycle.quiesce()
        with self.assertRaises(MaintenanceBlockedError):
            await self.lifecycle.resume(snapshot.maintenance_epoch, expected_revision=0)
        self.assertEqual(self.lifecycle.snapshot.revision, snapshot.revision)

    async def test_storage_write_failure_fences_admission(self):
        from unittest.mock import patch
        with patch.object(self.lifecycle.store, "save", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                await self.gate.begin_draining(1)
        with self.assertRaises(MaintenanceBlockedError):
            with self.gate.work("new", "denied"):
                pass
        self.assertEqual(self.lifecycle.store.read().state, S.RUNNING)  # never fabricate a commit

    async def test_missing_snapshot_is_not_reinitialized_as_running(self):
        from contextlib import closing
        from backup.lifecycle_store import LifecycleConflict
        await self.lifecycle.quiesce()
        self.lifecycle.close()
        with closing(sqlite3.connect(self.lifecycle.store.path)) as db, db:
            db.execute("DELETE FROM lifecycle_state")
        with self.assertRaises(LifecycleConflict):
            AgentHomeMaintenanceCoordinator(self.root, AgentHomeWriteGate())

    async def test_every_interrupted_state_survives_new_controller(self):
        await self.lifecycle.quiesce()
        await self.lifecycle.begin_restore(1)
        for expected in (S.RESTORING, S.RESUMING, S.FAILED):
            self.lifecycle.close()
            recovered = AgentHomeMaintenanceCoordinator(self.root, AgentHomeWriteGate())
            self.assertEqual(recovered.snapshot.state, expected)
            with self.assertRaises(MaintenanceBlockedError):
                async with recovered.gate.operation("new_run", "new"):
                    pass
            recovered.close()
            if expected == S.RESTORING:
                await self.gate.begin_resuming(1)
            elif expected == S.RESUMING:
                await self.gate.fail(1)

    async def test_thread_admission_is_atomic_with_transition(self):
        entered, release = threading.Event(), threading.Event()
        def work():
            with self.gate.work("worker", "thread"):
                entered.set()
                release.wait(5)
        task = asyncio.create_task(asyncio.to_thread(work))
        await asyncio.to_thread(entered.wait, 2)
        draining = asyncio.create_task(self.lifecycle.quiesce(timeout=3))
        while self.gate.state == S.RUNNING:
            await asyncio.sleep(0)
        def rejected():
            with self.assertRaises(MaintenanceBlockedError):
                with self.gate.work("worker", "late"):
                    pass
        await asyncio.to_thread(rejected)
        self.assertFalse(draining.done())
        release.set()
        await task
        await draining

    async def test_stale_contextvar_cannot_keep_a_finished_lease_alive(self):
        release = asyncio.Event()
        async def child():
            await release.wait()
            with self.assertRaises(MaintenanceBlockedError):
                async with self.gate.operation("tool", "stale", continuation=True):
                    pass
        async with self.gate.operation("run", "parent"):
            task = asyncio.create_task(child())
        await self.lifecycle.quiesce()
        release.set()
        await task

    async def test_subagent_body_cannot_start_from_an_existing_draining_run(self):
        from tools.subagent import SubagentTool
        from tool import AsyncToolRegistry, ToolContext

        runner = AsyncMock(return_value="done")
        tool = SubagentTool(runner, {}, AsyncToolRegistry([]))
        async with self.gate.operation("runtime_pool", "parent"):
            await self.gate.begin_draining(1)
            with self.assertRaises(MaintenanceBlockedError):
                await tool.run({"task": "child"}, ToolContext(project_root=self.root))
            runner.assert_not_awaited()

    async def test_non_idempotent_child_denied_before_body_is_skipped_not_unknown(self):
        from types import SimpleNamespace
        from uuid import uuid4
        from gateway.store import GatewayStore
        from gateway.state_controller import StateController
        from gateway.durable_execution import DurableToolCoordinator
        from Agent.state import TaskState, ExecutionState, WorkloadKind, TransitionCommand
        from tool import ToolContext

        store = GatewayStore(self.root / ".yy/gateway")
        project = store.register_project(self.root)
        controller = StateController(store.database_path, gateway_epoch="test", write_gate=self.gate)
        state, _ = controller.create_run(run_id=uuid4().hex, workload_kind=WorkloadKind.CHAT,
            project_id=project.project_id, client_id="test", task="test", idempotency_key=uuid4().hex,
            request_hash="a" * 64)
        for task_state, execution in ((TaskState.QUEUED, None), (TaskState.STARTING, None),
                                      (TaskState.RUNNING, ExecutionState.THINKING)):
            state = controller.apply(TransitionCommand(command_id=uuid4().hex,
                run_id=state.run_id, expected_revision=state.revision, gateway_epoch="test",
                task_state=task_state, execution_state=execution, reason="setup")).state
        coordinator = DurableToolCoordinator(controller)
        binding = coordinator.bind(state.run_id)
        body = AsyncMock(return_value="must not execute")
        try:
            async with self.gate.operation("runtime_pool", state.run_id):
                operation = await coordinator.prepare(tool=SimpleNamespace(idempotency="NON_IDEMPOTENT"),
                    name="harness_capability", arguments={}, risk="high",
                    context=ToolContext(project_root=self.root), tool_call_id="call-1")
                await self.gate.begin_draining(1)
                result = json.loads(await coordinator.execute(operation, body))
                self.assertEqual(result["reason"], "maintenance_new_work_denied")
                self.assertEqual(controller.current_attempt(operation.operation_id).status.value, "skipped")
                self.assertEqual(controller.state(state.run_id).task_state, TaskState.RUNNING)
                body.assert_not_awaited()
        finally:
            coordinator.reset(binding)


class GatewayMaintenanceIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_real_gateway_run_finalizes_while_quiescing(self):
        from tests.test_gateway import FakeRuntime
        from Agent.state import TaskState

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            release = asyncio.Event()
            closed_in = []
            class ClosingRuntime(FakeRuntime):
                async def close(self):
                    closed_in.append(app.write_gate.state)
                    app.write_gate.check_mutation_admission()
                    await super().close()
            app = GatewayApplication(load_runtime_config(root),
                runtime_factory=lambda workspace, approval: ClosingRuntime(workspace, release))
            await app.start()
            try:
                project = app.register_project(root)
                run = await app.start_run(RunCreateRequest(
                    project_id=project.project_id, client_id="test", task="drain me"))
                draining = asyncio.create_task(app.quiesce(timeout=10))
                while app.write_gate.state == S.RUNNING:
                    await asyncio.sleep(0)
                self.assertGreaterEqual(app.maintenance.snapshot.active_runs, 1)
                with self.assertRaises(MaintenanceBlockedError):
                    await app.start_run(RunCreateRequest(
                        project_id=project.project_id, client_id="test", task="not admitted"))
                release.set()
                await draining
                self.assertEqual(app.state_controller.state(run.run_id).task_state, TaskState.SUCCEEDED)
                self.assertEqual(app.maintenance.snapshot.state, S.QUIESCED)
                self.assertEqual(closed_in, [S.QUIESCING])
            finally:
                release.set()
                await app.close()

    async def test_interrupted_transition_boots_failed_and_requires_explicit_resume(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            app = GatewayApplication(load_runtime_config(root))
            await app.write_gate.begin_draining(1)
            await app.start()
            try:
                self.assertEqual(app.write_gate.state, S.FAILED)
                self.assertFalse(app._services_started)
                await app.resume(1, expected_revision=app.maintenance.snapshot.revision)
                self.assertTrue(app._services_started)
            finally:
                await app.close()

    async def test_service_entrypoints_and_restart_remain_closed(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            app = GatewayApplication(load_runtime_config(root))
            project = app.store.register_project(root)
            await app.quiesce(timeout=5)
            before = app.store.list_runs()
            with self.assertRaises(MaintenanceBlockedError):
                await app.start_run(RunCreateRequest(project_id=project.project_id, client_id="test", task="no"))
            for scheduler in (app.cron_scheduler, app.dream_scheduler):
                with self.assertRaises(MaintenanceBlockedError):
                    await scheduler.tick()
            self.assertEqual(app.store.list_runs(), before)
            revision = app.maintenance.snapshot.revision
            await app.close()
            restored = GatewayApplication(load_runtime_config(root))
            try:
                await restored.start()
                self.assertFalse(restored._services_started)
                self.assertEqual(restored.maintenance.snapshot.revision, revision)
                await restored.resume(1, expected_revision=revision)
                self.assertTrue(restored._services_started)
            finally:
                await restored.close()

    async def test_structured_control_ack_is_targeted_and_idempotent(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            app = GatewayApplication(load_runtime_config(root))
            manager = GatewayProcessManager(root)
            try:
                request = GatewayControlRequest(request_id="one", instance_id="owner", action="quiesce",
                    reason="test", requested_at=datetime.now().astimezone(), requested_by="test")
                manager.stop_request_path.write_text(request.model_dump_json(), encoding="utf-8")
                self.assertFalse(await process_control_request(manager, app, "wrong-owner"))
                self.assertEqual(app.write_gate.state, S.RUNNING)
                manager.stop_ack_path.unlink()
                manager.stop_ack_path.write_text("broken projection", encoding="utf-8")
                await process_control_request(manager, app, "owner")
                revision = app.maintenance.snapshot.revision
                await process_control_request(manager, app, "owner")
                self.assertEqual(app.maintenance.snapshot.revision, revision)
                self.assertEqual(json.loads(manager.stop_ack_path.read_text())["status"], "completed")
            finally:
                await app.close()


class MaintenanceAPITests(unittest.TestCase):
    def test_hard_exit_keeps_maintenance_closed(self):
        for state in ("quiescing", "quiesced", "restoring", "resuming"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as value:
                code = """
import asyncio, os, sys
from pathlib import Path
from backup.maintenance import AgentHomeWriteGate, AgentHomeMaintenanceCoordinator
async def main():
    c=AgentHomeMaintenanceCoordinator(Path(sys.argv[1]), AgentHomeWriteGate())
    if sys.argv[2] == 'quiescing':
        await c.gate.begin_draining(1)
    else:
        await c.quiesce()
        if sys.argv[2] in ('restoring','resuming'):
            await c.begin_restore(1)
        if sys.argv[2] == 'resuming':
            await c.gate.begin_resuming(1)
    os._exit(23)
asyncio.run(main())
"""
                result = subprocess.run([sys.executable, "-c", code, value, state],
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 23, result.stderr.decode(errors="replace"))
                c = AgentHomeMaintenanceCoordinator(Path(value), AgentHomeWriteGate())
                try:
                    self.assertEqual(c.snapshot.state.value, state)
                    with self.assertRaises(MaintenanceBlockedError):
                        with c.gate.work("new-run", "denied"):
                            pass
                finally:
                    c.close()

    def test_api_quiesce_rejects_mutation_and_resume_requires_exact_revision(self):
        with tempfile.TemporaryDirectory() as value:
            app = GatewayApplication(load_runtime_config(Path(value)))
            with TestClient(create_gateway_api(app, access_token="secret")) as client:
                headers = {"Authorization": "Bearer secret"}
                result = client.post("/api/v1/maintenance/quiesce", headers=headers, json={"timeout": 5})
                self.assertEqual(result.status_code, 200, result.text)
                state = result.json()
                self.assertEqual(state["state"], "quiesced")
                self.assertEqual(client.post("/api/v1/projects", headers=headers,
                                 json={"path": value}).status_code, 503)
                resumed = client.post("/api/v1/maintenance/resume", headers=headers, json={
                    "maintenance_epoch": state["maintenance_epoch"], "expected_revision": state["revision"]})
                self.assertEqual(resumed.status_code, 200, resumed.text)
                self.assertEqual(resumed.json()["state"], "running")
