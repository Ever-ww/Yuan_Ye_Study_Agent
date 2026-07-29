"""Gateway 业务编排入口。"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from time import monotonic

from Agent import RuntimeConfig
from gateway.events import GatewayEventBus
from gateway.models import ApprovalDecision, ProjectRecord, RunCreateRequest, RunRecord, SkillManageRequest
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
        self.pool = RuntimePool(
            agent_root=config.agent_root,
            store=self.store,
            events=self.events,
            max_concurrent_runs=config.gateway_max_concurrent_runs,
            idle_timeout_seconds=config.gateway_runtime_idle_seconds,
            runtime_factory=runtime_factory,
        )
        self._browser_codes: dict[str, float] = {}

    async def start(self) -> None:
        await self.pool.start()

    async def close(self) -> None:
        await self.pool.close()

    def register_project(self, path: Path, name: str | None = None) -> ProjectRecord:
        return self.store.register_project(path, name)

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
