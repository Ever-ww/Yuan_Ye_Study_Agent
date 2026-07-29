"""Gateway 协议、持久化、并发与断线审批测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from Agent import EventType, RunEvent, load_runtime_config
from gateway.api import create_gateway_api
from gateway.application import GatewayApplication
from gateway.approval import GatewayApprovalBroker
from gateway.events import GatewayEventBus
from gateway.models import RunCreateRequest
from gateway.runtime_pool import RuntimePool
from gateway.store import GatewayStore
from gateway.process import GatewayProcessManager, InstanceLock
from bootstrap import initialize_project, migrate_source_home
from memory import MemoryStore


class FakeRuntime:
    def __init__(self, workspace: Path, blocker: asyncio.Event | None = None) -> None:
        self.workspace = workspace
        self.blocker = blocker
        self.closed = 0

    async def run_task(self, task: str, session_id: str | None = None):
        selected = session_id or ("a" * 15 + str(abs(hash(task)) % 10))
        yield RunEvent(type=EventType.STARTED, payload={"session_id": selected})
        if self.blocker is not None:
            await self.blocker.wait()
        yield RunEvent(type=EventType.TEXT, payload={"content": "处理中"})
        yield RunEvent(type=EventType.FINAL, payload={"answer": f"完成：{task}"})

    async def close(self) -> None:
        self.closed += 1


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


class GatewayTests(unittest.TestCase):
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
            self.assertTrue(store.mark_inbox_read(item.item_id).read)

    def test_restart_marks_unfinished_run_interrupted_and_inboxed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            directory = root / ".yy" / "gateway"
            store = GatewayStore(directory)
            project = store.register_project(root)
            run = store.create_run(project.project_id, "client", "未完成任务", None)
            store.append_event(run.run_id, project.project_id, None, "run_queued", {})
            restarted = GatewayStore(directory)
            self.assertEqual(restarted.run(run.run_id).status, "interrupted")
            self.assertEqual(restarted.read_events(run.run_id)[-1].type, "run_interrupted")
            self.assertEqual(restarted.list_inbox()[0].run_id, run.run_id)

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
            pool = RuntimePool(
                agent_root=root,
                store=store,
                events=GatewayEventBus(),
                max_concurrent_runs=4,
                idle_timeout_seconds=30,
                runtime_factory=lambda workspace, approval: FakeRuntime(workspace, blocker),
            )
            await pool.start()
            first = store.create_run(project.project_id, "one", "first", "b" * 16)
            second = store.create_run(project.project_id, "two", "second", "b" * 16)
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
                waiting = asyncio.create_task(broker("write_file", {"path": "demo.txt"}))
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
                return await broker("write_file", {"path": "demo.txt"})
            finally:
                broker.reset_run(token)

        with tempfile.TemporaryDirectory() as value:
            self.assertFalse(asyncio.run(check(Path(value))))

    def test_global_concurrency_is_capped_at_four_and_fifo_waits(self) -> None:
        async def check(root: Path) -> tuple[int, list[str]]:
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            tracker = ConcurrencyTracker()
            pool = RuntimePool(
                agent_root=root,
                store=store,
                events=GatewayEventBus(),
                max_concurrent_runs=4,
                idle_timeout_seconds=30,
                runtime_factory=lambda workspace, approval: TrackingRuntime(workspace, tracker),
            )
            await pool.start()
            runs = [
                store.create_run(project.project_id, f"client-{index}", f"task-{index}", None)
                for index in range(5)
            ]
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
            migrate_source_home(source, target)
            workspace_key = hashlib.sha256(
                os.path.normcase(str(source.resolve())).encode(),
            ).hexdigest()[:16]
            partition = target / ".yy" / "memory" / "session" / workspace_key
            self.assertTrue((source / ".yy" / "memory" / "session" / "index.json").exists())
            self.assertTrue((partition / "index.json").exists())
            self.assertIn(session_id, (partition / "index.json").read_text(encoding="utf-8"))
            self.assertTrue((target / "skills" / "demo" / "SKILL.md").exists())
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
            second.acquire()
            second.close()

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
