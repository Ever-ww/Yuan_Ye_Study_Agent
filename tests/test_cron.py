from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cron import (
    CronJobCreateRequest,
    CronPaperResearchPresetRequest,
    CronSchedule,
    CronScheduleCalculator,
    CronScheduler,
    CronService,
    CronStore,
)
from tool import AsyncToolRegistry
from tools import CronJobTool
from Agent import RuntimeConfig
from Agent import AgentRuntime
from Agent.contracts import ModelReply
from Agent.state import PersistenceContract
from bootstrap import initialize_project
from gateway.api import create_gateway_api
from gateway.application import GatewayApplication
from gateway.approval import GatewayApprovalBroker
from gateway.store import GatewayStore
from gateway.models import RunRecord
from memory import MemoryStore
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
        self.assertTrue(self.store.database_path.is_file())
        self.assertFalse(self.store.path.exists())
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
        self.assertEqual(triggered.next_run_at, before_trigger)
        history = await self.service.history(job.job_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].trigger, "run_now")
        self.assertEqual(history[0].status, "pending")
        removed = await self.service.remove(job.job_id)
        self.assertEqual(removed.state, "deleted")
        self.assertEqual((await self.service.history(job.job_id))[0].status, "cancelled")

    async def test_corrupt_json_is_unhealthy_without_replacement(self) -> None:
        legacy_root = self.root / "legacy-corrupt"
        legacy_store = CronStore(legacy_root)
        legacy_store.path.parent.mkdir(parents=True, exist_ok=True)
        legacy_store.path.write_text("{broken", encoding="utf-8")
        status = await CronService(legacy_store).status()
        self.assertFalse(status.healthy)
        self.assertIn("legacy jobs.json invalid", status.last_error or "")
        self.assertEqual(legacy_store.path.read_text(encoding="utf-8"), "{broken")

    async def test_paper_research_preset_is_idempotent_and_persisted(self) -> None:
        first = await self.service.ensure_paper_research_preset(
            project_id="project",
            expression="0 9 * * 1",
            timezone_name="Asia/Shanghai",
        )
        second = await self.service.ensure_paper_research_preset(
            project_id="project",
            expression="0 9 * * 1",
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(
            first.preapproved_tools,
            ("paper_library_download", "paper_library_save", "reference_write"),
        )
        jobs = await self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, first.job_id)
        self.assertFalse(self.store.path.exists())

    async def test_cron_preapproval_rejects_recursive_and_self_modifying_tools(self) -> None:
        with self.assertRaisesRegex(ValueError, "harness_evolve"):
            await self.service.create(CronJobCreateRequest(
                project_id="project",
                name="unsafe",
                prompt="modify yourself",
                schedule=CronSchedule(kind="interval", interval_seconds=3600),
                preapproved_tools=("harness_evolve",),
            ))

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

    async def test_cron_uses_only_exact_durable_tool_grants(self) -> None:
        async def publish(request):
            raise AssertionError("unattended Cron must not publish an interactive approval")

        async def authorize(job_id, tool_name):
            return job_id == "job" and tool_name == "paper_library_save"

        gateway_store = GatewayStore(self.root / ".yy" / "gateway-grant-test")
        broker = GatewayApprovalBroker(
            gateway_store,
            publish,
            cron_tool_authorizer=authorize,
        )
        token = broker.bind_run("run", "cron:job")
        try:
            self.assertTrue(await broker("paper_library_save", {}))
            self.assertFalse(await broker("write", {}))
        finally:
            broker.reset_run(token)

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

        async def submit(dispatch):
            run_id = f"run-{dispatch.dispatch_id}"
            self.submitted.append((dispatch.job_id, run_id))
            await self.store.bind_running(
                dispatch.dispatch_id,
                claim_token=str(dispatch.claim_token),
                session_id=f"session-{dispatch.dispatch_id}",
                run_id=run_id,
                operation_id=f"operation-{dispatch.dispatch_id}",
                attempt_id=f"attempt-{dispatch.dispatch_id}",
            )

        self.scheduler = CronScheduler(
            self.store,
            submit,
            gateway_epoch=lambda: "test-gateway-epoch",
            clock=lambda: self.now,
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

        current = await self.store.job(job.job_id)
        due = current.model_copy(update={
            "next_run_at": (self.now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "revision": current.revision + 1,
        })
        await self.store.save_job(due, expected_revision=current.revision)
        return due

    async def test_due_job_runs_once_and_advances_beyond_now(self) -> None:
        job = await self._add_due()
        runs = await self.scheduler.tick()
        self.assertEqual(len(runs), 1)
        current = await self.store.job(job.job_id)
        self.assertEqual(current.current_run_id, runs[0])
        self.assertGreater(datetime.fromisoformat(current.next_run_at.replace("Z", "+00:00")), self.now)
        await self.scheduler.tick()
        self.assertEqual(len(self.submitted), 1)

    async def test_overlap_is_skipped_and_terminal_run_is_settled(self) -> None:
        job = await self._add_due()
        run_id = (await self.scheduler.tick())[0]
        current = await self.store.job(job.job_id)
        due = current.model_copy(update={
            "next_run_at": (self.now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "revision": current.revision + 1,
        })
        await self.store.save_job(due, expected_revision=current.revision)
        await self.scheduler.tick()
        skipped = await self.store.job(job.job_id)
        self.assertEqual(skipped.overlap_skipped, 1)
        self.assertEqual(len(self.submitted), 1)

        running = await self.store.dispatch_by_run_id(run_id)
        await self.store.mark_terminal(
            running.dispatch_id,
            status="succeeded",
            result="done",
            error=None,
            completed_at=self.now.isoformat().replace("+00:00", "Z"),
        )
        settled = await self.store.job(job.job_id)
        self.assertIsNone(settled.current_run_id)
        self.assertEqual(settled.run_count, 1)
        self.assertEqual(settled.last_session_id, f"session-{running.dispatch_id}")

    async def test_legacy_json_is_not_re_read_after_migration(self) -> None:
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("not-json", encoding="utf-8")
        await self.scheduler.start()
        self.assertIsNone(self.scheduler.last_error)
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
            self.assertIsInstance(cron_runtime.memory, MemoryStore)
            self.assertEqual(cron_runtime.memory.prompt_context("any"), "")
            cron_runtime.memory.create_session("", session_id="c0ffee0000000001")
            cron_prompt = cron_runtime.prompts.system.open_session("c0ffee0000000001").content
            self.assertNotIn("无记忆 Cron", cron_prompt)
            cron_query = cron_runtime.prompts.render_provider_query(
                "check",
                "c0ffee0000000001",
            )
            self.assertIn("无记忆 Cron", cron_query)
            self.assertNotIn("search-summary-paper", cron_runtime.memory.prompt_context("any"))
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
                preset = client.post(
                    "/api/v1/cron/presets/paper-research",
                    headers=headers,
                    json=CronPaperResearchPresetRequest(
                        project_id=project.project_id,
                        expression="0 9 * * 1",
                        timezone="Asia/Shanghai",
                    ).model_dump(mode="json"),
                )
                self.assertEqual(preset.status_code, 200, preset.text)
                duplicate = client.post(
                    "/api/v1/cron/presets/paper-research",
                    headers=headers,
                    json={
                        "project_id": project.project_id,
                        "expression": "0 9 * * 1",
                        "timezone": "Asia/Shanghai",
                    },
                )
                self.assertEqual(duplicate.json()["job_id"], preset.json()["job_id"])
                blocked = client.delete(
                    f"/api/v1/projects/{project.project_id}", headers=headers,
                )
                self.assertEqual(blocked.status_code, 409)
                removed = client.delete(f"/api/v1/cron/jobs/{job_id}", headers=headers)
                self.assertEqual(removed.status_code, 200)
                self.assertEqual(
                    client.delete(
                        f"/api/v1/cron/jobs/{preset.json()['job_id']}", headers=headers,
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    client.delete(f"/api/v1/projects/{project.project_id}", headers=headers).status_code,
                    200,
                )


class CronGatewayRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_dispatch_gets_a_fresh_durable_session_and_inbox_result(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            initialize_project(root)
            workspace = root / "workspace"
            workspace.mkdir()
            config = RuntimeConfig(agent_root=root, workspace_root=workspace)

            class AnswerProvider:
                streaming = False

                async def complete(self, messages, tools):
                    del messages, tools
                    return ModelReply(text="scheduled result")

            def runtime_factory(selected_workspace, approval):
                return AgentRuntime(
                    config.model_copy(update={"workspace_root": selected_workspace}),
                    provider=AnswerProvider(),
                    tools=AsyncToolRegistry(),
                    approval=approval,
                    enable_sandbox=False,
                    enable_skills=False,
                    enable_context_processing=False,
                    enable_extensions=False,
                    session_origin="cron",
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
                session_ids: set[str] = set()
                for _ in range(2):
                    await gateway.run_cron(job.job_id)
                    run_id = (await gateway.cron_scheduler.tick())[0]
                    for _ in range(500):
                        run = gateway.store.run(run_id)
                        if run.status in {"completed", "failed", "cancelled", "interrupted"}:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(run.status, "completed")
                    self.assertIsNotNone(run.session_id)
                    session_ids.add(str(run.session_id))
                    self.assertEqual(
                        gateway.state_controller.state(run_id).persistence_contract,
                        PersistenceContract.SESSION_BACKED_WORKLOAD,
                    )
                    dispatch = await gateway.cron_store.dispatch_by_run_id(run_id)
                    self.assertEqual(dispatch.status, "succeeded")
                    self.assertEqual(dispatch.session_id, run.session_id)
                    await gateway.cron_scheduler.tick()
                self.assertEqual(len(session_ids), 2)
                inbox_runs = {item.run_id for item in gateway.store.list_inbox()}
                self.assertGreaterEqual(len(inbox_runs), 2)
            finally:
                await gateway.pool.close()


if __name__ == "__main__":
    unittest.main()
