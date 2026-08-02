from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cron import (
    CronJobCreateRequest,
    CronSchedule,
    CronScheduleCalculator,
    CronScheduler,
    CronService,
    CronState,
    CronStore,
)
from tool import AsyncToolRegistry
from tools import CronJobTool
from Agent import RuntimeConfig
from Agent import AgentRuntime
from bootstrap import initialize_project
from gateway.api import create_gateway_api
from gateway.application import GatewayApplication
from gateway.approval import GatewayApprovalBroker
from gateway.store import GatewayStore
from gateway.models import RunRecord
from run_ui.cli import _handle_cron_command, _parse_interval


class CronScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calculator = CronScheduleCalculator()

    def test_five_field_features_and_preview(self) -> None:
        schedules = (
            "*/15 * * * *",
            "0 9 * JAN,MAR MON-FRI",
            "1-10/2 8 * * 0,7",
            "0 9 1 * MON",
        )
        for expression in schedules:
            schedule = self.calculator.validate(expression, "Asia/Shanghai")
            self.assertEqual(len(schedule.expression.split()), 5)
            self.assertTrue(self.calculator.preview(schedule).next_runs)

    def test_rejects_nonstandard_and_unknown_timezone(self) -> None:
        for expression in (
            "@daily", "0 0 0 * * *", "0 0 1 1 * 2027",
            "0 0 L * *", "0 0 * * MON#2", "0 0 ? * *",
        ):
            with self.assertRaises(ValueError):
                self.calculator.validate(expression, "UTC")
        with self.assertRaises(ValueError):
            self.calculator.validate("0 9 * * *", "Mars/Olympus")

    def test_dst_gap_is_skipped_and_fold_runs_once(self) -> None:
        spring = CronSchedule(kind="cron", expression="0 2 * * *", timezone="America/New_York")
        values = self.calculator.preview(
            spring,
            count=3,
            base_time=datetime(2026, 3, 7, tzinfo=timezone.utc),
        ).next_runs
        self.assertEqual(values[:2], ("2026-03-07T07:00:00Z", "2026-03-09T06:00:00Z"))

        autumn = CronSchedule(kind="cron", expression="30 1 * * *", timezone="America/New_York")
        values = self.calculator.preview(
            autumn,
            count=3,
            base_time=datetime(2026, 10, 31, tzinfo=timezone.utc),
        ).next_runs
        self.assertEqual(values.count("2026-11-01T05:30:00Z"), 1)
        self.assertNotIn("2026-11-01T06:30:00Z", values)

    def test_interval_keeps_original_timeline(self) -> None:
        schedule = CronSchedule(kind="interval", interval_seconds=1800)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = self.calculator.next_future(schedule, base, base + timedelta(hours=2, minutes=2))
        self.assertEqual(result, base + timedelta(hours=2, minutes=30))

    def test_day_of_month_and_weekday_use_or_semantics(self) -> None:
        schedule = CronSchedule(kind="cron", expression="0 9 15 * MON", timezone="UTC")
        values = self.calculator.preview(
            schedule,
            count=2,
            base_time=datetime(2026, 6, 2, tzinfo=timezone.utc),
        ).next_runs
        self.assertEqual(values, ("2026-06-08T09:00:00Z", "2026-06-15T09:00:00Z"))


class CronStoreServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CronStore(self.root)
        self.service = CronService(self.store)
        await self.store.ensure()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_initializes_and_manages_jobs(self) -> None:
        self.assertTrue((self.root / ".yy" / "cron" / "jobs.json").is_file())
        job = await self.service.create(CronJobCreateRequest(
            project_id="project",
            name="daily report",
            prompt="Summarize the project",
            schedule=CronSchedule(kind="cron", expression="0 9 * * 1-5", timezone="Asia/Shanghai"),
        ))
        self.assertEqual((await self.service.list("project"))[0].job_id, job.job_id)
        self.assertEqual((await self.service.pause(job.job_id)).state, "paused")
        self.assertEqual((await self.service.resume(job.job_id)).state, "scheduled")
        before_trigger = (await self.service.get(job.job_id)).next_run_at
        triggered = await self.service.trigger(job.job_id)
        self.assertTrue(triggered.manual_run_requested)
        self.assertEqual(triggered.next_run_at, before_trigger)
        await self.store.mutate(lambda state: setattr(
            state.jobs[job.job_id], "manual_run_requested", False,
        ))
        removed = await self.service.remove(job.job_id)
        self.assertEqual(removed.job_id, job.job_id)

    async def test_corrupt_json_is_unhealthy_without_replacement(self) -> None:
        self.store.path.write_text("{broken", encoding="utf-8")
        status = await self.service.status()
        self.assertFalse(status.healthy)
        self.assertIn("损坏", status.last_error or "")
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{broken")

    async def test_cronjob_tool_is_dynamic_and_cannot_enter_subagent_subset(self) -> None:
        tool = CronJobTool(self.service, "project")
        self.assertEqual(tool.risk_for({"action": "list"}), "read")
        self.assertEqual(tool.risk_for({"action": "create"}), "high")
        registry = AsyncToolRegistry([tool])
        with self.assertRaises(ValueError):
            registry.select(["cronjob"])

    async def test_cron_client_dangerous_approval_is_rejected_immediately(self) -> None:
        published = []

        async def publish(request):
            published.append(request)

        gateway_store = GatewayStore(self.root / ".yy" / "gateway-test")
        broker = GatewayApprovalBroker(gateway_store, publish)
        token = broker.bind_run("run", "cron:job")
        try:
            self.assertFalse(await broker("write", {"path": "x"}))
        finally:
            broker.reset_run(token)
        self.assertEqual(published, [])

    async def test_interactive_cron_command_uses_gateway_without_session_turn(self) -> None:
        class FakeClient:
            request = None

            async def create_cron(self, request):
                self.request = request
                return SimpleNamespace(job_id="cron-created")

        client = FakeClient()
        await _handle_cron_command(
            client,
            "project",
            '/cron add --cron "0 9 * * 1-5" --timezone Asia/Shanghai '
            '--name "AI 简报" --prompt "搜索 AI 新闻并总结"',
        )
        self.assertEqual(client.request.project_id, "project")
        self.assertEqual(client.request.schedule.expression, "0 9 * * 1-5")
        self.assertEqual(_parse_interval("30m"), 1800)


class CronSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CronStore(self.root)
        await self.store.ensure()
        self.now = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        self.submitted: list[tuple[str, str]] = []
        self.runs: dict[str, SimpleNamespace] = {}

        async def submit(job, run_id):
            self.submitted.append((job.job_id, run_id))
            self.runs[run_id] = SimpleNamespace(
                status="queued", session_id=None, error=None, finished_at=None,
            )

        def lookup(run_id):
            if run_id not in self.runs:
                raise KeyError(run_id)
            return self.runs[run_id]

        self.scheduler = CronScheduler(
            self.store, submit, lookup, clock=lambda: self.now,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _add_due(self, interval: int = 300):
        service = CronService(self.store)
        job = await service.create(CronJobCreateRequest(
            project_id="project",
            name="poll",
            prompt="poll status",
            schedule=CronSchedule(kind="interval", interval_seconds=interval),
        ))

        def due(state: CronState) -> None:
            state.jobs[job.job_id] = state.jobs[job.job_id].model_copy(update={
                "next_run_at": (self.now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            })
        await self.store.mutate(due)
        return job

    async def test_due_job_runs_once_and_advances_beyond_now(self) -> None:
        job = await self._add_due()
        runs = await self.scheduler.tick()
        self.assertEqual(len(runs), 1)
        current = (await self.store.load()).jobs[job.job_id]
        self.assertEqual(current.active_run_id, runs[0])
        self.assertGreater(datetime.fromisoformat(current.next_run_at.replace("Z", "+00:00")), self.now)
        await self.scheduler.tick()
        self.assertEqual(len(self.submitted), 1)

    async def test_overlap_is_skipped_and_terminal_run_is_settled(self) -> None:
        job = await self._add_due()
        run_id = (await self.scheduler.tick())[0]
        current = (await self.store.load()).jobs[job.job_id]

        def make_due(state: CronState) -> None:
            state.jobs[job.job_id] = current.model_copy(update={
                "next_run_at": (self.now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            })
        await self.store.mutate(make_due)
        await self.scheduler.tick()
        skipped = (await self.store.load()).jobs[job.job_id]
        self.assertEqual(skipped.skipped_overlap_count, 1)
        self.assertEqual(len(self.submitted), 1)

        self.runs[run_id] = SimpleNamespace(
            status="completed", session_id="fresh-session", error=None,
            finished_at=self.now.isoformat().replace("+00:00", "Z"),
        )
        await self.scheduler.tick()
        settled = (await self.store.load()).jobs[job.job_id]
        self.assertIsNone(settled.active_run_id)
        self.assertEqual(settled.run_count, 1)
        self.assertEqual(settled.last_session_id, "fresh-session")

    async def test_corrupt_state_does_not_prevent_scheduler_lifecycle(self) -> None:
        self.store.path.write_text("not-json", encoding="utf-8")
        await self.scheduler.start()
        self.assertIn("损坏", self.scheduler.last_error or "")
        await self.scheduler.close()


class CronGatewayApiTests(unittest.TestCase):
    def test_cron_management_api_and_project_protection(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            initialize_project(root)
            config = RuntimeConfig(agent_root=root, workspace_root=root)
            gateway = GatewayApplication(config)
            workspace = root / "workspace"
            workspace.mkdir()
            project = gateway.register_project(workspace)
            base_run = {
                "run_id": "runtime-check",
                "project_id": project.project_id,
                "session_id": None,
                "task": "check",
                "status": "queued",
                "created_at": "2026-08-03T00:00:00+00:00",
            }
            normal_runtime = gateway.pool._default_runtime(
                workspace, gateway.pool.approvals,
                RunRecord(client_id="client", **base_run),
            )
            cron_runtime = gateway.pool._default_runtime(
                workspace, gateway.pool.approvals,
                RunRecord(client_id="cron:job", **{**base_run, "run_id": "cron-runtime-check"}),
            )
            self.assertIn("cronjob", normal_runtime.tools.names())
            self.assertNotIn("cronjob", cron_runtime.tools.names())
            asyncio.run(normal_runtime.close())
            asyncio.run(cron_runtime.close())
            app = create_gateway_api(gateway, access_token="cron-test-token")
            headers = {"Authorization": "Bearer cron-test-token"}
            with TestClient(app) as client:
                response = client.post("/api/v1/cron/jobs", headers=headers, json={
                    "project_id": project.project_id,
                    "name": "hourly",
                    "prompt": "summarize",
                    "schedule": {"kind": "interval", "interval_seconds": 3600},
                })
                self.assertEqual(response.status_code, 200, response.text)
                job_id = response.json()["job_id"]
                self.assertEqual(
                    client.get("/api/v1/cron/jobs", headers=headers).json()[0]["job_id"],
                    job_id,
                )
                preview = client.post("/api/v1/cron/preview", headers=headers, json={
                    "schedule": {
                        "kind": "cron", "expression": "0 9 * * 1-5", "timezone": "Asia/Shanghai",
                    },
                    "count": 5,
                })
                self.assertEqual(len(preview.json()["next_runs"]), 5)
                blocked = client.delete(
                    f"/api/v1/projects/{project.project_id}", headers=headers,
                )
                self.assertEqual(blocked.status_code, 409)
                removed = client.delete(f"/api/v1/cron/jobs/{job_id}", headers=headers)
                self.assertEqual(removed.status_code, 200)
                self.assertEqual(
                    client.delete(f"/api/v1/projects/{project.project_id}", headers=headers).status_code,
                    200,
                )


class CronGatewayRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_trigger_creates_fresh_session_and_inbox_result(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            initialize_project(root)
            workspace = root / "workspace"
            workspace.mkdir()
            config = RuntimeConfig(agent_root=root, workspace_root=workspace)

            def runtime_factory(selected_workspace, approval):
                return AgentRuntime(
                    config.model_copy(update={"workspace_root": selected_workspace}),
                    tools=AsyncToolRegistry(),
                    approval=approval,
                    enable_sandbox=False,
                    enable_skills=False,
                    enable_context_processing=False,
                    enable_extensions=False,
                )

            gateway = GatewayApplication(config, runtime_factory=runtime_factory)
            project = gateway.register_project(workspace)
            await gateway.pool.start()
            try:
                job = await gateway.create_cron(CronJobCreateRequest(
                    project_id=project.project_id,
                    name="fresh session",
                    prompt="scheduled hello",
                    schedule=CronSchedule(kind="interval", interval_seconds=3600),
                ))
                sessions: list[str] = []
                for _ in range(2):
                    await gateway.run_cron(job.job_id)
                    run_id = (await gateway.cron_scheduler.tick())[0]
                    for _ in range(200):
                        run = gateway.store.run(run_id)
                        if run.status in {"completed", "failed", "cancelled", "interrupted"}:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(run.status, "completed")
                    self.assertIsNotNone(run.session_id)
                    sessions.append(str(run.session_id))
                    await gateway.cron_scheduler.tick()
                self.assertNotEqual(sessions[0], sessions[1])
                inbox_runs = {item.run_id for item in gateway.store.list_inbox()}
                self.assertTrue(set(sessions))
                self.assertGreaterEqual(len(inbox_runs), 2)
            finally:
                await gateway.pool.close()


if __name__ == "__main__":
    unittest.main()
