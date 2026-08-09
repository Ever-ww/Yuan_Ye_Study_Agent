"""Gateway 协议、持久化、并发与断线审批测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from Agent import EventType, RunEvent, load_runtime_config
from Agent.state import TaskState, TransitionCommand, WorkloadKind
from skill import SkillRefreshResult
from gateway.api import create_gateway_api
from gateway.application import GatewayApplication
from gateway.approval import GatewayApprovalBroker
from gateway.client import GatewayClient
from gateway.events import GatewayEventBus
from gateway.models import (
    CodeFinalizeResult,
    CodeSessionRecord,
    CodeTurnResult,
    GatewayEventEnvelope,
    RunCreateRequest,
)
from gateway.runtime_pool import RuntimeEntry, RuntimePool
from gateway.state_controller import StateController
from gateway.store import GatewayStore
from gateway.process import (
    GatewayProcessManager,
    InstanceLock,
    _GatewayProtocolNoiseFilter,
    _is_benign_closed_h11_response,
    _pid_alive,
    _windows_background_creationflags,
    run_gateway,
)
from bootstrap import (
    initialize_project,
    legacy_gateway_active,
    migrate_source_home,
    platform_agent_home,
)
from memory import MemoryStore
from sandbox import SandboxStatus


class FakeRuntime:
    def __init__(self, workspace: Path, blocker: asyncio.Event | None = None) -> None:
        self.workspace = workspace
        self.blocker = blocker
        self.closed = 0
        self.skill_refreshes: list[str] = []

    async def run_task(self, task: str, session_id: str | None = None):
        selected = session_id or ("a" * 15 + str(abs(hash(task)) % 10))
        yield RunEvent(type=EventType.STARTED, payload={"session_id": selected})
        if self.blocker is not None:
            await self.blocker.wait()
        yield RunEvent(type=EventType.TEXT, payload={"content": "处理中"})
        yield RunEvent(type=EventType.FINAL, payload={"answer": f"完成：{task}"})

    async def close(self) -> None:
        self.closed += 1

    async def refresh_skills(self, session_id: str) -> SkillRefreshResult:
        self.skill_refreshes.append(session_id)
        return SkillRefreshResult(
            status="unchanged",
            message="unchanged",
            session_id=session_id,
        )


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()


class TrackingRuntime(FakeRuntime):
    def __init__(self, workspace: Path, tracker: ConcurrencyTracker) -> None:
        super().__init__(workspace)
        self.tracker = tracker

    async def run_task(self, task: str, session_id: str | None = None):
        selected = session_id or hashlib.sha256(task.encode()).hexdigest()[:16]
        yield RunEvent(type=EventType.STARTED, payload={"session_id": selected})
        self.tracker.active += 1
        self.tracker.maximum = max(self.tracker.maximum, self.tracker.active)
        if self.tracker.active >= 4:
            self.tracker.started.set()
        try:
            await self.tracker.release.wait()
            yield RunEvent(type=EventType.FINAL, payload={"answer": task})
        finally:
            self.tracker.active -= 1


class FakeCodeSessions:
    async def start(self, project_id: str, client_id: str):
        return CodeSessionRecord(
            code_session_id="c" * 32,
            project_id=project_id,
            client_id=client_id,
            source_root="D:/source",
            worktree_path="D:/agent/.yy/worktree",
            branch="harness-code/test",
            base_commit="a" * 40,
            status="active",
            verified_turns=0,
        )

    async def run_turn(self, session_id: str, client_id: str, task: str):
        del client_id, task
        return CodeTurnResult(
            code_session_id=session_id,
            status="verified",
            message="ok",
            test_file="tests/extensions/test_x.py",
            attempts=1,
            commit="b" * 40,
        )

    async def finalize(self, session_id: str, client_id: str):
        del client_id
        return CodeFinalizeResult(
            code_session_id=session_id,
            status="merged",
            message="merged",
            merged=True,
        )

    async def abort(self, session_id: str, client_id: str):
        del client_id
        return CodeFinalizeResult(
            code_session_id=session_id,
            status="aborted",
            message="aborted",
        )

    def events(self, session_id: str, after_sequence: int = 0):
        del session_id
        return [
            {"version": 1, "sequence": 2, "record_type": "code_test"}
        ] if after_sequence < 2 else []

    async def close(self) -> None:
        return None


class GatewayTests(unittest.TestCase):
    def test_skill_refresh_uses_long_request_timeout(self) -> None:
        async def check() -> None:
            client = object.__new__(GatewayClient)
            captured: dict[str, object] = {}

            async def request(method: str, path: str, **kwargs):
                captured.update(method=method, path=path, **kwargs)
                return {"status": "unchanged"}

            client._request = request
            result = await client.refresh_skills("project", "session")
            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(captured["timeout"], 3600)

        asyncio.run(check())

    def test_terminal_subscription_closes_nested_event_generator(self) -> None:
        async def check() -> None:
            client = object.__new__(GatewayClient)
            closed = asyncio.Event()
            terminal = GatewayEventEnvelope(
                event_id="event-1",
                sequence=1,
                timestamp="2026-08-03T22:00:00+08:00",
                project_id="project-1",
                run_id="run-1",
                type="run_completed",
                payload={"answer": "done"},
            )

            async def events(run_id: str, *, after_sequence: int = 0):
                del run_id, after_sequence
                try:
                    yield terminal
                finally:
                    closed.set()

            client.events = events
            received = [event async for event in client.subscribe("run-1")]
            self.assertEqual(received, [terminal])
            self.assertTrue(closed.is_set())

        asyncio.run(check())

    def test_cancelled_subscription_explicitly_closes_nested_event_generator(self) -> None:
        async def check() -> None:
            client = object.__new__(GatewayClient)
            closed = asyncio.Event()
            event = GatewayEventEnvelope(
                event_id="event-1",
                sequence=1,
                timestamp="2026-08-03T22:00:00+08:00",
                project_id="project-1",
                run_id="run-1",
                type="text",
                payload={"content": "partial"},
            )

            async def events(run_id: str, *, after_sequence: int = 0):
                del run_id, after_sequence
                try:
                    yield event
                    await asyncio.Future()
                finally:
                    closed.set()

            client.events = events
            subscription = client.subscribe("run-1")
            self.assertEqual(await anext(subscription), event)
            await subscription.aclose()
            self.assertTrue(closed.is_set())

        asyncio.run(check())

    def test_default_agent_home_is_user_home_and_override_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            with (
                patch.dict(os.environ, {"YY_AGENT_HOME": ""}),
                patch("bootstrap.home.Path.home", return_value=home),
            ):
                self.assertEqual(platform_agent_home(), home.resolve())
            override = home / "custom"
            with patch.dict(os.environ, {"YY_AGENT_HOME": str(override)}):
                self.assertEqual(platform_agent_home(), override.resolve())

    def test_legacy_gateway_lock_defers_live_state_migration(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            lock = InstanceLock(root / ".yy" / "gateway" / "instance.lock")
            lock.acquire()
            try:
                self.assertTrue(legacy_gateway_active(root))
            finally:
                lock.close()
            self.assertFalse(legacy_gateway_active(root))

    def test_windows_gateway_background_flags_never_allocate_console(self) -> None:
        flags = _windows_background_creationflags()
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        self.assertEqual(flags & detached, 0)
        self.assertEqual(flags & no_window, no_window)
        self.assertEqual(flags & process_group, process_group)

    def test_pid_probe_works_for_current_and_missing_process(self) -> None:
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(2_147_483_647))

    def test_gateway_process_manager_uses_no_window_flags_and_closes_handles(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value), 18768)
            with (
                patch.object(manager, "_healthy", side_effect=[False, False, True, True]),
                patch("gateway.process._port_available", return_value=True),
                patch("gateway.process.os.name", "nt"),
                patch("gateway.process.subprocess.Popen") as popen,
            ):
                status = manager.ensure_running(timeout_seconds=0.2)
            self.assertTrue(status["running"])
            options = popen.call_args.kwargs
            self.assertEqual(options["creationflags"], _windows_background_creationflags())
            self.assertTrue(options["close_fds"])
            self.assertFalse(options["start_new_session"])
            self.assertEqual(options["env"]["PYTHONUTF8"], "1")
            self.assertEqual(options["env"]["PYTHONIOENCODING"], "utf-8")

    def test_concurrent_gateway_starter_waits_instead_of_spawning_again(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value), 18769)
            first = InstanceLock(manager.startup_lock_path)
            first.acquire()
            try:
                with (
                    patch.object(manager, "_healthy", side_effect=[False, True, True]),
                    patch("gateway.process.subprocess.Popen") as popen,
                ):
                    status = manager.ensure_running(timeout_seconds=0.2)
            finally:
                first.close()
            self.assertTrue(status["running"])
            popen.assert_not_called()

    def test_unhealthy_locked_instance_is_not_replaced_by_another_child(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value), 18771)
            existing = InstanceLock(manager.lock_path)
            existing.acquire()
            try:
                with (
                    patch.object(manager, "_healthy", return_value=False),
                    patch("gateway.process.subprocess.Popen") as popen,
                ):
                    with self.assertRaisesRegex(RuntimeError, "持有状态锁"):
                        manager.ensure_running(timeout_seconds=0.05)
            finally:
                existing.close()
            popen.assert_not_called()

    def test_closed_h11_response_noise_is_filtered_narrowly(self) -> None:
        import h11

        closed = h11.LocalProtocolError(
            "can't handle event type Response when role=SERVER and state=CLOSED"
        )
        self.assertTrue(_is_benign_closed_h11_response({"exception": closed}))
        self.assertFalse(_is_benign_closed_h11_response({"exception": RuntimeError("closed")}))

        protocol_filter = _GatewayProtocolNoiseFilter()
        noisy = logging.LogRecord(
            "uvicorn.error", logging.WARNING, __file__, 1,
            "Invalid HTTP request received.", (), None,
        )
        unrelated = logging.LogRecord(
            "uvicorn.error", logging.WARNING, __file__, 1,
            "Application error", (), None,
        )
        self.assertFalse(protocol_filter.filter(noisy))
        self.assertTrue(protocol_filter.filter(unrelated))

    def test_duplicate_gateway_child_exits_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            with patch.object(InstanceLock, "acquire", side_effect=RuntimeError("held")):
                self.assertIsNone(run_gateway(Path(value), 18770))

    def test_gateway_status_reports_checkpoint_only_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            application = GatewayApplication(
                load_runtime_config(root),
                runtime_factory=lambda workspace, approval: FakeRuntime(workspace),
            )
            fallback = SandboxStatus(
                mode="checkpoint_only",
                bash_available=False,
                reason_code="docker_daemon_unavailable",
                message="Docker daemon 无法连接",
            )
            with patch("gateway.api.probe_docker_status", AsyncMock(return_value=fallback)):
                with TestClient(create_gateway_api(application, access_token="test-token")) as client:
                    response = client.get(
                        "/api/v1/status",
                        headers={"Authorization": "Bearer test-token"},
                    )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertFalse(payload["sandbox"])
            self.assertEqual(payload["sandbox_mode"], "checkpoint_only")
            self.assertFalse(payload["bash_available"])
            self.assertEqual(payload["sandbox_reason"], fallback.message)

    def test_code_session_api_create_turn_events_and_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            application = GatewayApplication(
                load_runtime_config(root),
                runtime_factory=lambda workspace, approval: FakeRuntime(workspace),
            )
            application.code_sessions = FakeCodeSessions()
            project = application.register_project(root)
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(create_gateway_api(application, access_token="test-token")) as client:
                created = client.post(
                    "/api/v1/code/sessions",
                    headers=headers,
                    json={"project_id": project.project_id, "client_id": "code-client"},
                )
                self.assertEqual(created.status_code, 200)
                session_id = created.json()["code_session_id"]
                turn = client.post(
                    f"/api/v1/code/sessions/{session_id}/turns",
                    headers=headers,
                    json={"client_id": "code-client", "task": "新增审计扩展"},
                )
                self.assertEqual(turn.json()["status"], "verified")
                events = client.get(
                    f"/api/v1/code/sessions/{session_id}/events",
                    headers=headers,
                )
                self.assertEqual(events.json()[0]["record_type"], "code_test")
                finalized = client.post(
                    f"/api/v1/code/sessions/{session_id}/finalize",
                    headers=headers,
                    params={"client_id": "code-client"},
                )
                self.assertTrue(finalized.json()["merged"])

    def test_store_persists_replayable_monotonic_events_and_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            run = store.create_run(project.project_id, "client", "任务", None)
            first = store.append_event(run.run_id, project.project_id, None, "run_started", {})
            second = store.append_event(run.run_id, project.project_id, "a" * 16, "text", {"content": "好"})
            self.assertEqual((first.sequence, second.sequence), (1, 2))
            self.assertEqual([event.sequence for event in store.read_events(run.run_id, 1)], [2])
            completed = store.update_run(
                run.run_id,
                session_id="a" * 16,
                status="completed",
                answer="完成",
            )
            item = store.create_inbox(completed)
            self.assertFalse(item.read)
            self.assertTrue(store.mark_run_inbox_read(run.run_id).read)
            self.assertTrue(store.mark_inbox_read(item.item_id).read)

    def test_store_restart_does_not_guess_unfinished_run_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            directory = root / ".yy" / "gateway"
            store = GatewayStore(directory)
            project = store.register_project(root)
            run = store.create_run(project.project_id, "client", "未完成任务", None)
            store.append_event(run.run_id, project.project_id, None, "run_queued", {})
            restarted = GatewayStore(directory)
            self.assertEqual(restarted.run(run.run_id).status, "queued")
            self.assertEqual(restarted.read_events(run.run_id)[-1].type, "run_queued")
            self.assertEqual(restarted.list_inbox(), [])

    def test_api_runs_through_gateway_and_replays_events(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, gateway_runtime_idle_seconds=30)
            application = GatewayApplication(
                config,
                runtime_factory=lambda workspace, approval: FakeRuntime(workspace),
            )
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(create_gateway_api(application, access_token="test-token")) as client:
                project_response = client.post(
                    "/api/v1/projects",
                    headers=headers,
                    json={"path": str(root)},
                )
                self.assertEqual(project_response.status_code, 200)
                project_id = project_response.json()["project_id"]
                run_response = client.post(
                    "/api/v1/runs",
                    headers=headers,
                    json={
                        "project_id": project_id,
                        "client_id": "test-client",
                        "task": "测试 Gateway",
                        "session_id": None,
                    },
                )
                self.assertEqual(run_response.status_code, 200)
                run_id = run_response.json()["run_id"]
                for _ in range(100):
                    current = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
                    if current["status"] == "completed":
                        break
                    import time
                    time.sleep(0.01)
                self.assertEqual(current["status"], "completed")
                events = client.get(
                    f"/api/v1/runs/{run_id}/events",
                    headers=headers,
                    params={"after_sequence": 1},
                ).json()
                self.assertTrue(events)
                self.assertEqual(events[-2]["type"], "run_completed")
                self.assertEqual(events[-1]["type"], "inbox_created")
                inbox = client.get("/api/v1/inbox", headers=headers).json()
                self.assertEqual(inbox[0]["run_id"], run_id)
                self.assertFalse(inbox[0]["read"])
                terminal_sequence = next(
                    event["sequence"] for event in events if event["type"] == "run_completed"
                )
                with client.websocket_connect(
                    f"/api/v1/events?token=test-token&client_id=test-client"
                    f"&run_id={run_id}&after_sequence={terminal_sequence - 1}"
                ) as socket:
                    delivered = socket.receive_json()
                self.assertEqual(delivered["type"], "run_completed")
                inbox = client.get("/api/v1/inbox", headers=headers).json()
                self.assertTrue(inbox[0]["read"])
                with client.websocket_connect(
                    f"/api/v1/events?token=test-token&client_id=replay-client"
                    f"&run_id={run_id}&after_sequence=1"
                ) as socket:
                    replayed = socket.receive_json()
                self.assertEqual(replayed["sequence"], 2)

    def test_gateway_cancel_marks_run_and_creates_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            blocker = asyncio.Event()
            application = GatewayApplication(
                load_runtime_config(root, gateway_runtime_idle_seconds=30),
                runtime_factory=lambda workspace, approval: FakeRuntime(workspace, blocker),
            )
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(create_gateway_api(application, access_token="test-token")) as client:
                project = client.post(
                    "/api/v1/projects",
                    headers=headers,
                    json={"path": str(root)},
                ).json()
                run = client.post(
                    "/api/v1/runs",
                    headers=headers,
                    json={
                        "project_id": project["project_id"],
                        "client_id": "cancel-client",
                        "task": "等待取消",
                        "session_id": None,
                    },
                ).json()
                for _ in range(100):
                    current = client.get(f"/api/v1/runs/{run['run_id']}", headers=headers).json()
                    if current["status"] == "running":
                        break
                    import time
                    time.sleep(0.01)
                cancelled = client.post(
                    f"/api/v1/runs/{run['run_id']}/cancel",
                    headers=headers,
                )
                self.assertTrue(cancelled.json()["cancelled"])
                for _ in range(100):
                    current = client.get(f"/api/v1/runs/{run['run_id']}", headers=headers).json()
                    if current["status"] == "cancelled":
                        break
                    time.sleep(0.01)
                self.assertEqual(current["status"], "cancelled")
                inbox = client.get("/api/v1/inbox", headers=headers).json()
                self.assertEqual(inbox[0]["run_id"], run["run_id"])

    def test_api_auth_browser_csrf_and_origin(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            application = GatewayApplication(load_runtime_config(root))
            app = create_gateway_api(application, access_token="test-token")
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/projects").status_code, 401)
                code = application.issue_browser_code()
                exchange = client.post("/api/v1/browser/exchange", json={"code": code})
                self.assertEqual(exchange.status_code, 200)
                csrf = exchange.json()["csrf"]
                self.assertEqual(
                    client.post("/api/v1/projects", json={"path": str(root)}).status_code,
                    403,
                )
                accepted = client.post(
                    "/api/v1/projects",
                    headers={"X-CSRF-Token": csrf},
                    json={"path": str(root)},
                )
                self.assertEqual(accepted.status_code, 200)
                rejected_origin = client.post(
                    "/api/v1/projects",
                    headers={"X-CSRF-Token": csrf, "Origin": "https://example.com"},
                    json={"path": str(root)},
                )
                self.assertEqual(rejected_origin.status_code, 403)

    def test_same_session_is_rejected_while_running(self) -> None:
        async def check(root: Path) -> None:
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            blocker = asyncio.Event()
            controller = StateController(store.database_path, gateway_epoch="test")
            pool = RuntimePool(
                agent_root=root,
                store=store,
                events=GatewayEventBus(),
                max_concurrent_runs=4,
                idle_timeout_seconds=30,
                runtime_factory=lambda workspace, approval: FakeRuntime(workspace, blocker),
                state_controller=controller,
            )
            await pool.start()
            run_ids = (uuid4().hex, uuid4().hex)
            for run_id, client, task in zip(run_ids, ("one", "two"), ("first", "second")):
                state, _ = controller.create_run(
                    run_id=run_id, workload_kind=WorkloadKind.CHAT,
                    project_id=project.project_id, client_id=client, task=task,
                    session_id="b" * 16, idempotency_key=uuid4().hex,
                    request_hash=hashlib.sha256(task.encode()).hexdigest(),
                )
                controller.apply(TransitionCommand(
                    command_id=uuid4().hex, run_id=run_id,
                    expected_revision=state.revision, gateway_epoch="test",
                    task_state=TaskState.QUEUED, reason="test queue",
                ))
            first, second = (store.run(run_id) for run_id in run_ids)
            await pool.submit(first)
            with self.assertRaisesRegex(RuntimeError, "同一个 Session"):
                await pool.submit(second)
            blocker.set()
            for _ in range(100):
                if store.run(first.run_id).status == "completed":
                    break
                await asyncio.sleep(0.01)
            await pool.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_skill_refresh_targets_only_requested_session_runtime(self) -> None:
        async def check(root: Path) -> None:
            store = GatewayStore(root / ".yy" / "gateway")
            controller = StateController(store.database_path, gateway_epoch="test")
            pool = RuntimePool(
                agent_root=root,
                store=store,
                events=GatewayEventBus(),
                state_controller=controller,
            )
            first, second = FakeRuntime(root), FakeRuntime(root)
            pool._runtimes[("project", "a" * 16)] = RuntimeEntry(first, 0.0)
            pool._runtimes[("project", "b" * 16)] = RuntimeEntry(second, 0.0)

            result = await pool.refresh_skills("project", "a" * 16)

            self.assertEqual(result.session_id, "a" * 16)
            self.assertEqual(first.skill_refreshes, ["a" * 16])
            self.assertEqual(second.skill_refreshes, [])

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_skill_refresh_restores_idle_session_without_chat_message(self) -> None:
        async def check(root: Path) -> None:
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            memory = MemoryStore(
                root / ".yy" / "memory",
                workspace_root=root,
                agent_root=root,
            )
            session_id = memory.create_session("hello")
            controller = StateController(store.database_path, gateway_epoch="test")
            created: list[FakeRuntime] = []

            def factory(workspace: Path, approvals) -> FakeRuntime:
                del approvals
                runtime = FakeRuntime(workspace)
                created.append(runtime)
                return runtime

            pool = RuntimePool(
                agent_root=root,
                store=store,
                events=GatewayEventBus(),
                state_controller=controller,
                runtime_factory=factory,
            )
            result = await pool.refresh_skills(project.project_id, session_id)

            self.assertEqual(result.session_id, session_id)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].skill_refreshes, [session_id])
            self.assertIn((project.project_id, session_id), pool._runtimes)
            await pool.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_client_disconnect_denies_pending_approval(self) -> None:
        async def check(root: Path) -> bool:
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            run = store.create_run(project.project_id, "origin-client", "写文件", None)
            published = asyncio.Event()

            async def publish(request) -> None:
                self.assertEqual(request.run_id, run.run_id)
                published.set()

            broker = GatewayApprovalBroker(store, publish)
            token = broker.bind_run(run.run_id, "origin-client")
            try:
                waiting = asyncio.create_task(broker("write", {"path": "demo.txt"}))
                await published.wait()
                self.assertEqual(await broker.deny_client("origin-client"), 1)
                return await waiting
            finally:
                broker.reset_run(token)

        with tempfile.TemporaryDirectory() as value:
            self.assertFalse(asyncio.run(check(Path(value))))

    def test_approval_is_denied_when_origin_client_never_connects(self) -> None:
        async def check(root: Path) -> bool:
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            run = store.create_run(project.project_id, "missing-client", "写文件", None)

            async def publish(request) -> None:
                self.assertEqual(request.client_id, "missing-client")

            async def disconnected(client_id: str) -> bool:
                self.assertEqual(client_id, "missing-client")
                return False

            broker = GatewayApprovalBroker(store, publish, disconnected)
            token = broker.bind_run(run.run_id, "missing-client")
            try:
                return await broker("write", {"path": "demo.txt"})
            finally:
                broker.reset_run(token)

        with tempfile.TemporaryDirectory() as value:
            self.assertFalse(asyncio.run(check(Path(value))))

    def test_global_concurrency_is_capped_at_four_and_fifo_waits(self) -> None:
        async def check(root: Path) -> tuple[int, list[str]]:
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            tracker = ConcurrencyTracker()
            controller = StateController(store.database_path, gateway_epoch="test")
            pool = RuntimePool(
                agent_root=root,
                store=store,
                events=GatewayEventBus(),
                max_concurrent_runs=4,
                idle_timeout_seconds=30,
                runtime_factory=lambda workspace, approval: TrackingRuntime(workspace, tracker),
                state_controller=controller,
            )
            await pool.start()
            runs = []
            for index in range(5):
                run_id = uuid4().hex
                task = f"task-{index}"
                state, _ = controller.create_run(
                    run_id=run_id, workload_kind=WorkloadKind.CHAT,
                    project_id=project.project_id, client_id=f"client-{index}", task=task,
                    idempotency_key=uuid4().hex,
                    request_hash=hashlib.sha256(task.encode()).hexdigest(),
                )
                controller.apply(TransitionCommand(
                    command_id=uuid4().hex, run_id=run_id,
                    expected_revision=state.revision, gateway_epoch="test",
                    task_state=TaskState.QUEUED, reason="test queue",
                ))
                runs.append(store.run(run_id))
            for run in runs:
                await pool.submit(run)
            await asyncio.wait_for(tracker.started.wait(), 2)
            statuses = [store.run(run.run_id).status for run in runs]
            tracker.release.set()
            for _ in range(200):
                if all(store.run(run.run_id).status == "completed" for run in runs):
                    break
                await asyncio.sleep(0.01)
            await pool.close()
            return tracker.maximum, statuses

        with tempfile.TemporaryDirectory() as value:
            maximum, statuses = asyncio.run(check(Path(value)))
            self.assertEqual(maximum, 4)
            self.assertEqual(statuses.count("running"), 4)
            self.assertEqual(statuses.count("queued"), 1)

    def test_agent_home_migration_is_copy_only_and_partitions_legacy_session(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            source, target = base / "source", base / "home"
            initialize_project(source)
            memory = MemoryStore(
                source / ".yy" / "memory",
                workspace_root=source,
                agent_root=source,
            )
            session_id = memory.create_session("迁移")
            memory.record_user(session_id, "迁移")
            (source / "skills" / "demo").mkdir(parents=True)
            (source / "skills" / "demo" / "SKILL.md").write_text("demo", encoding="utf-8")
            (target / ".yy").mkdir(parents=True)
            (target / ".yy" / "settings.local.json").write_text(
                "目标配置不得覆盖",
                encoding="utf-8",
            )
            migrate_source_home(source, target)
            workspace_key = hashlib.sha256(
                os.path.normcase(str(source.resolve())).encode(),
            ).hexdigest()[:16]
            partition = target / ".yy" / "memory" / "session" / workspace_key
            self.assertTrue((source / ".yy" / "memory" / "session" / "index.json").exists())
            self.assertTrue((partition / "index.json").exists())
            self.assertIn(session_id, (partition / "index.json").read_text(encoding="utf-8"))
            self.assertTrue(
                (target / ".yy" / "skills" / "installed" / "demo" / "SKILL.md").exists()
            )
            self.assertEqual(
                (target / ".yy" / "settings.local.json").read_text(encoding="utf-8"),
                "目标配置不得覆盖",
            )
            self.assertTrue((target / ".yy" / "agent-home-migration.json").exists())

    def test_instance_lock_rejects_second_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            lock_path = Path(value) / "instance.lock"
            first = InstanceLock(lock_path)
            second = InstanceLock(lock_path)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.close()
            self.assertEqual(lock_path.read_text(encoding="ascii").strip(), str(os.getpid()))
            second.acquire()
            second.close()

    def test_status_preserves_unhealthy_owner_metadata_for_stop(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value), 18772)
            owner = InstanceLock(manager.lock_path)
            owner.acquire()
            manager.instance_path.write_text(
                json.dumps({"pid": os.getpid(), "port": 18772}),
                encoding="utf-8",
            )
            try:
                with patch.object(manager, "_healthy", return_value=False):
                    status = manager.status()
                self.assertFalse(status["running"])
                self.assertEqual(status["pid"], os.getpid())
                self.assertTrue(manager.instance_path.exists())
            finally:
                owner.close()

    def test_gateway_log_rotation_keeps_bounded_backups(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value), 18765)
            manager.log_path.write_text("current", encoding="utf-8")
            manager.log_path.with_name("gateway.log.1").write_text("older", encoding="utf-8")
            manager._rotate_logs(max_bytes=1, backups=2)
            self.assertFalse(manager.log_path.exists())
            self.assertEqual(
                manager.log_path.with_name("gateway.log.1").read_text(encoding="utf-8"),
                "current",
            )
            self.assertEqual(
                manager.log_path.with_name("gateway.log.2").read_text(encoding="utf-8"),
                "older",
            )

    def test_gateway_status_removes_stale_instance_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manager = GatewayProcessManager(Path(value), 18767)
            manager.instance_path.write_text(
                json.dumps({"pid": 999999, "port": 18767}),
                encoding="utf-8",
            )
            status = manager.status()
            self.assertFalse(status["running"])
            self.assertIsNone(status["pid"])
            self.assertFalse(manager.instance_path.exists())

    def test_gateway_models_are_strict(self) -> None:
        with self.assertRaises(Exception):
            RunCreateRequest.model_validate({
                "project_id": "p",
                "client_id": "c",
                "task": "x",
                "unexpected": True,
            }, strict=True)
