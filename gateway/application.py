"""Gateway 业务编排入口。"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from time import monotonic

from Agent import ExtensionLoader, RuntimeConfig
from cron import (
    CronJob,
    CronJobCreateRequest,
    CronJobEditRequest,
    CronSchedule,
    CronScheduleCalculator,
    CronScheduler,
    CronService,
    CronStore,
)
from gateway.code_sessions import CodeSessionManager
from gateway.events import GatewayEventBus
from gateway.models import (
    ApprovalDecision,
    CodeSessionCreateRequest,
    CodeTurnRequest,
    ProjectRecord,
    RunCreateRequest,
    RunRecord,
    SkillManageRequest,
)
from gateway.runtime_pool import RuntimeFactory, RuntimePool
from gateway.store import GatewayStore
from memory import MemoryStore
from skill import SkillInstallRequest, SkillService


class GatewayApplication:
    """集中拥有 Runtime、项目、运行、审批、Inbox 和事件广播。"""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        runtime_factory: RuntimeFactory | None = None,
        store: GatewayStore | None = None,
    ) -> None:
        self.config = config
        self.store = store or GatewayStore(config.agent_root / ".yy" / "gateway")
        self.events = GatewayEventBus()
        source_root = config.coding_source_root or Path(__file__).resolve().parents[1]
        self.extensions = ExtensionLoader(source_root).scan()
        self.cron_store = CronStore(
            config.agent_root,
            heartbeat_seconds=config.cron_heartbeat_seconds,
        )
        self.cron_service = CronService(self.cron_store, CronScheduleCalculator())
        self.pool = RuntimePool(
            agent_root=config.agent_root,
            store=self.store,
            events=self.events,
            max_concurrent_runs=config.gateway_max_concurrent_runs,
            idle_timeout_seconds=config.gateway_runtime_idle_seconds,
            runtime_factory=runtime_factory,
            extensions=self.extensions,
            cron_service=self.cron_service,
        )
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            self._submit_cron_run,
            self.store.run,
        )
        self.cron_service.set_waker(self.cron_scheduler.wake)
        self._browser_codes: dict[str, float] = {}
        self.code_sessions = CodeSessionManager(config)

    async def start(self) -> None:
        await self.pool.start()
        await self.cron_scheduler.start()

    async def close(self) -> None:
        await self.cron_scheduler.close()
        await self.code_sessions.close()
        await self.pool.close()

    async def start_code_session(self, request: CodeSessionCreateRequest):
        self.store.project(request.project_id)
        return await self.code_sessions.start(request.project_id, request.client_id)

    async def run_code_turn(self, session_id: str, request: CodeTurnRequest):
        return await self.code_sessions.run_turn(session_id, request.client_id, request.task)

    async def finalize_code_session(self, session_id: str, client_id: str):
        return await self.code_sessions.finalize(session_id, client_id)

    async def abort_code_session(self, session_id: str, client_id: str):
        return await self.code_sessions.abort(session_id, client_id)

    def code_session_events(self, session_id: str, after_sequence: int = 0):
        return self.code_sessions.events(session_id, after_sequence)

    def register_project(self, path: Path, name: str | None = None) -> ProjectRecord:
        return self.store.register_project(path, name)

    async def remove_project(self, project_id: str) -> None:
        if await self.cron_service.project_has_jobs(project_id):
            raise RuntimeError("项目仍有关联 Cron Job，必须先删除计划任务")
        self.store.remove_project(project_id)

    async def create_cron(self, request: CronJobCreateRequest) -> CronJob:
        self.store.project(request.project_id)
        result = await self.cron_service.create(request)
        self.cron_scheduler.wake()
        return result

    async def edit_cron(self, job_id: str, request: CronJobEditRequest) -> CronJob:
        current = await self.cron_service.get(job_id)
        self.store.project(current.project_id)
        result = await self.cron_service.edit(job_id, request)
        self.cron_scheduler.wake()
        return result

    async def pause_cron(self, job_id: str) -> CronJob:
        result = await self.cron_service.pause(job_id)
        self.cron_scheduler.wake()
        return result

    async def resume_cron(self, job_id: str) -> CronJob:
        result = await self.cron_service.resume(job_id)
        self.cron_scheduler.wake()
        return result

    async def run_cron(self, job_id: str) -> CronJob:
        result = await self.cron_service.trigger(job_id)
        self.cron_scheduler.wake()
        return result

    async def remove_cron(self, job_id: str) -> CronJob:
        result = await self.cron_service.remove(job_id)
        self.cron_scheduler.wake()
        return result

    async def cron_status(self):
        return await self.cron_service.status(error=self.cron_scheduler.last_error)

    def cron_preview(self, schedule: CronSchedule, count: int = 5):
        return self.cron_service.preview(schedule, count=count)

    async def start_run(self, request: RunCreateRequest) -> RunRecord:
        self.store.project(request.project_id)
        run = self.store.create_run(
            request.project_id,
            request.client_id,
            request.task,
            request.session_id,
        )
        try:
            await self.pool.submit(run)
        except Exception:
            self.store.update_run(
                run.run_id,
                status="failed",
                finished_at=_now(),
                error="任务未能进入运行队列",
            )
            raise
        return run

    async def _submit_cron_run(self, job: CronJob, run_id: str) -> None:
        self.store.project(job.project_id)
        run = self.store.create_run(
            job.project_id,
            f"cron:{job.job_id}",
            job.prompt,
            None,
            run_id=run_id,
        )
        await self.pool.submit(run)

    async def cancel_run(self, run_id: str) -> bool:
        self.store.run(run_id)
        return await self.pool.cancel(run_id)

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> bool:
        return await self.pool.approvals.decide(
            approval_id,
            decision.client_id,
            decision.approved,
        )

    async def disconnect_client(self, client_id: str) -> int:
        return await self.pool.approvals.deny_client(client_id)

    def sessions(self, project_id: str) -> list[dict[str, object]]:
        project = self.store.project(project_id)
        memory = MemoryStore(
            self.config.memory_dir,
            workspace_root=Path(project.path),
            agent_root=self.config.agent_root,
        )
        return memory.list_sessions()

    def session_records(self, project_id: str, session_id: str) -> list[dict[str, object]]:
        project = self.store.project(project_id)
        memory = MemoryStore(
            self.config.memory_dir,
            workspace_root=Path(project.path),
            agent_root=self.config.agent_root,
        )
        return memory.session_records(session_id)

    def skills(self, project_id: str) -> SkillService:
        project = self.store.project(project_id)
        return SkillService(
            self.config.agent_root,
            Path(project.path),
        )

    async def manage_skill(self, request: SkillManageRequest):
        project = self.store.project(request.project_id)

        async def approve(name: str, arguments: dict) -> bool:
            del name, arguments
            return request.confirmed

        service = SkillService(
            self.config.agent_root,
            Path(project.path),
            approval=approve,
        )
        result = await service.install(SkillInstallRequest(
            source=request.source,
            action=request.action,
            ref=request.ref,
            skill_path=request.skill_path,
            name=request.name,
        ))
        return result

    def issue_browser_code(self, ttl_seconds: int = 60) -> str:
        now = monotonic()
        self._browser_codes = {
            code: expires for code, expires in self._browser_codes.items() if expires > now
        }
        code = secrets.token_urlsafe(32)
        self._browser_codes[code] = now + ttl_seconds
        return code

    def consume_browser_code(self, code: str) -> bool:
        expires = self._browser_codes.pop(code, None)
        return expires is not None and expires > monotonic()


def _now() -> str:
    from gateway.models import now_iso
    return now_iso()
