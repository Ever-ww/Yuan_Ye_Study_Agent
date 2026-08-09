"""Gateway 业务编排入口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from Agent import ExtensionLoader, RuntimeConfig
from Agent.state import (
    CompleteFinalizeStepCommand,
    FinalizeInboxCommand,
    FinalizeTerminalCommand,
    RecordRuntimeEventCommand,
    RecoveryDecisionCommand,
    TaskState,
    TerminalTarget,
    TransitionCommand,
    WorkloadKind,
    is_runnable,
)
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
from gateway.outbox import OutboxDispatcher
from gateway.recovery import RecoveryCoordinator
from gateway.state_controller import StateController
from gateway.models import (
    ApprovalDecision,
    CodeSessionCreateRequest,
    CodeTurnRequest,
    ProjectRecord,
    RunCreateRequest,
    RecoveryDecisionRequest,
    RunRecord,
    SkillManageRequest,
)
from gateway.runtime_pool import RuntimeFactory, RuntimePool
from gateway.store import GatewayStore
from memory import MemoryStore
from reference import (
    ReferenceEmbeddingWorker,
    ReferenceService,
    ReferenceStore,
    build_embedding_provider,
)
from skill import SkillInstallRequest, SkillService
from dream import DreamRunResult, DreamScheduler, DreamService
from backup import (
    AgentHomeMaintenanceCoordinator,
    AgentHomeWriteGate,
    BackupScheduler,
    BackupService,
    SensitiveEnvSanitizer,
    assert_restore_inactive,
)


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
        assert_restore_inactive(config.agent_root)
        self.write_gate = AgentHomeWriteGate()
        self.maintenance = AgentHomeMaintenanceCoordinator(config.agent_root, self.write_gate)
        # Backup passphrase is consumed before Runtime/Tool/Harness construction and
        # never enters RuntimeConfig or ToolContext.
        self._backup_passphrase = SensitiveEnvSanitizer.consume_backup_passphrase()
        self.store = store or GatewayStore(config.agent_root / ".yy" / "gateway")
        self.events = GatewayEventBus()
        self.gateway_epoch = uuid4().hex
        self.state_controller = StateController(
            self.store.database_path,
            gateway_epoch=self.gateway_epoch,
            migration_backup_path=self.store.migration_backup_path,
            write_gate=self.write_gate,
        )
        self.outbox = OutboxDispatcher(
            self.store.database_path,
            self.store.runs_directory,
            self.events.publish,
            retry_max_attempts=config.outbox_retry_max_attempts,
            retry_base_seconds=config.outbox_retry_base_seconds,
            retry_max_seconds=config.outbox_retry_max_seconds,
            dead_letter_enabled=config.outbox_dead_letter_enabled,
        )
        source_root = config.coding_source_root or Path(__file__).resolve().parents[1]
        self.extensions = ExtensionLoader(source_root).scan()
        self.cron_store = CronStore(
            config.agent_root,
            heartbeat_seconds=config.cron_heartbeat_seconds,
        )
        self.cron_service = CronService(self.cron_store, CronScheduleCalculator())
        self.reference_store = ReferenceStore(config.reference_database_path)
        self.reference_embedding_provider = build_embedding_provider(config)
        self.reference_embedding_worker = ReferenceEmbeddingWorker(
            self.reference_store,
            self.reference_embedding_provider,
        )
        self.reference_service = ReferenceService(
            self.reference_store,
            self.reference_embedding_provider,
            keyword_weight=config.reference_keyword_weight,
            semantic_weight=config.reference_semantic_weight,
            worker=self.reference_embedding_worker,
        )
        self.pool = RuntimePool(
            agent_root=config.agent_root,
            store=self.store,
            events=self.events,
            max_concurrent_runs=config.gateway_max_concurrent_runs,
            idle_timeout_seconds=config.gateway_runtime_idle_seconds,
            approval_timeout_seconds=config.approval_timeout_seconds,
            runtime_factory=runtime_factory,
            extensions=self.extensions,
            cron_service=self.cron_service,
            reference_service=self.reference_service,
            state_controller=self.state_controller,
            outbox=self.outbox,
            tool_retry_max_attempts=config.tool_retry_max_attempts,
            tool_retry_base_seconds=config.tool_retry_base_seconds,
            tool_retry_max_seconds=config.tool_retry_max_seconds,
            write_gate=self.write_gate,
        )
        self.recovery = RecoveryCoordinator(self.state_controller, self.store, self.pool.submit)
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            self._submit_cron_run,
            self.store.run,
            write_gate=self.write_gate,
        )
        self.cron_service.set_waker(self.cron_scheduler.wake)
        self.dream_service = DreamService(
            config,
            excluded_sessions=self.store.automated_session_ids,
        )
        self.dream_scheduler = DreamScheduler(
            self.dream_service,
            self.pool.is_idle,
            self._record_dream_result,
            heartbeat_seconds=config.cron_heartbeat_seconds,
            run_day=lambda selected: self._execute_dream_day(selected, automatic=True),
            write_gate=self.write_gate,
        )
        self._browser_codes: dict[str, float] = {}
        self.code_sessions = CodeSessionManager(config)
        self.maintenance.register("runtime_pool", self.pool)
        self.maintenance.register("cron", self.cron_scheduler)
        self.maintenance.register("dream", self.dream_scheduler)
        self.maintenance.register("outbox", self.outbox)
        self.maintenance.register("reference_embedding", self.reference_embedding_worker)
        self.maintenance.register("harness", self.code_sessions)
        self.backup_service = BackupService(
            config.agent_root,
            coordinator=self.maintenance,
            secret_provider=lambda: self._backup_passphrase,
            backup_directory=config.backup_directory,
            source_root=source_root,
            retention_daily=config.backup_retention_daily,
            retention_weekly=config.backup_retention_weekly,
            retention_monthly=config.backup_retention_monthly,
            min_free_space_bytes=config.backup_min_free_space_bytes,
            max_storage_bytes=config.backup_max_storage_bytes,
        )
        self.backup_scheduler = BackupScheduler(
            self.backup_service,
            self.write_gate,
            enabled=config.backup_enabled,
            schedule=config.backup_schedule,
            timezone=config.backup_timezone,
            drain_timeout_seconds=config.backup_drain_timeout_seconds,
            on_result=self._record_backup_result,
            heartbeat_seconds=config.cron_heartbeat_seconds,
        )

    async def start(self) -> None:
        self.state_controller.prune_retention()
        await self.outbox.start()
        await self.reference_embedding_worker.start()
        try:
            await self.pool.start()
            await self.recovery.recover()
            await self.cron_scheduler.start()
            await self.dream_scheduler.start()
            await self.backup_scheduler.start()
        except Exception:
            await self.outbox.close()
            await self.reference_embedding_worker.close()
            raise

    async def create_backup(self, passphrase: str, output: Path | None = None):
        return await self.backup_service.create(
            passphrase=passphrase,
            output=output,
            kind="manual",
            drain_timeout_seconds=self.config.backup_drain_timeout_seconds,
        )

    async def _record_backup_result(self, status: str, payload: dict[str, object]) -> None:
        run_id = uuid4().hex
        state = self._begin_workload_run(
            run_id=run_id,
            workload=WorkloadKind.MAINTENANCE,
            project_id="backup",
            client_id="backup:scheduler",
            task="Automatic Agent Home backup",
        )
        target = TerminalTarget.SUCCEEDED if status == "backup_completed" else TerminalTarget.FAILED
        state = self.state_controller.apply(RecordRuntimeEventCommand(
            command_id=uuid4().hex,
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            event_type=status,
            payload=payload,
            mark_progress=True,
        )).state
        self.state_controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            task_state=TaskState.FINALIZING,
            terminal_target=target,
            reason="Backup maintenance进入FINALIZING",
            error=str(payload.get("message", "")) if target is TerminalTarget.FAILED else None,
            result_summary=str(payload.get("path", "Backup completed")) if target is TerminalTarget.SUCCEEDED else None,
        ))
        self._finalize_control_plane(
            run_id, target,
            str(payload.get("message") or payload.get("path") or status),
            task_title="Automatic Agent Home backup",
            create_visible_inbox=True,
        )
        self.outbox.wake()

    async def close(self) -> None:
        try:
            await self.backup_scheduler.close()
            await self.dream_scheduler.close()
            await self.cron_scheduler.close()
            await self.code_sessions.close()
            await self.pool.close()
        finally:
            await self.reference_embedding_worker.close()
            await self.outbox.close()

    async def start_code_session(self, request: CodeSessionCreateRequest):
        self.store.project(request.project_id)
        return await self._run_code_workload(
            WorkloadKind.CODE_SESSION_START,
            request.project_id,
            request.client_id,
            "启动 Coding Session",
            lambda: self.code_sessions.start(request.project_id, request.client_id),
        )

    async def run_code_turn(self, session_id: str, request: CodeTurnRequest):
        project_id = self._code_project(session_id)
        return await self._run_code_workload(
            WorkloadKind.CODE_TURN,
            project_id,
            request.client_id,
            request.task,
            lambda: self.code_sessions.run_turn(session_id, request.client_id, request.task),
        )

    async def finalize_code_session(self, session_id: str, client_id: str):
        project_id = self._code_project(session_id)
        return await self._run_code_workload(
            WorkloadKind.CODE_FINALIZE,
            project_id,
            client_id,
            "合并并结束 Coding Session",
            lambda: self.code_sessions.finalize(session_id, client_id),
        )

    async def abort_code_session(self, session_id: str, client_id: str):
        project_id = self._code_project(session_id)
        return await self._run_code_workload(
            WorkloadKind.CODE_ABORT,
            project_id,
            client_id,
            "放弃 Coding Session",
            lambda: self.code_sessions.abort(session_id, client_id),
        )

    async def _run_code_workload(self, kind, project_id, client_id, task, operation):
        state = self._begin_workload_run(
            run_id=uuid4().hex,
            workload=kind,
            project_id=project_id,
            client_id=client_id,
            task=task,
        )
        if not is_runnable(state, None, now=datetime.now().astimezone()):
            raise RuntimeError(f"Code workload 不可调度：{state.task_state.value}")
        try:
            result = await operation()
        except Exception as exc:
            self._finish_workload(state.run_id, TerminalTarget.FAILED, str(exc) or type(exc).__name__)
            raise
        self._finish_workload(state.run_id, TerminalTarget.SUCCEEDED, "Coding workload 完成")
        return result

    def _finish_workload(self, run_id: str, target: TerminalTarget, summary: str) -> None:
        state = self.state_controller.state(run_id)
        state = self.state_controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            task_state=TaskState.FINALIZING,
            terminal_target=target,
            reason="非 Agent workload 进入 FINALIZING",
            error=summary if target is TerminalTarget.FAILED else None,
            result_summary=summary if target is TerminalTarget.SUCCEEDED else None,
        )).state
        self._finalize_control_plane(run_id, target, summary, task_title="Coding workload")
        self.outbox.wake()

    def _finalize_control_plane(
        self,
        run_id: str,
        target: TerminalTarget,
        summary: str,
        *,
        task_title: str,
        create_visible_inbox: bool = True,
    ) -> None:
        for step_name in ("memory", "session_index", "audit"):
            state = self.state_controller.state(run_id)
            self.state_controller.apply(CompleteFinalizeStepCommand(
                command_id=f"finalize:{run_id}:{step_name}", run_id=run_id,
                expected_revision=state.revision, gateway_epoch=self.gateway_epoch,
                step_name=step_name, result="completed",
            ))
        state = self.state_controller.state(run_id)
        if create_visible_inbox:
            operation_id = hashlib.sha256(f"{run_id}:finalize:inbox".encode("utf-8")).hexdigest()
            self.state_controller.apply(FinalizeInboxCommand(
                command_id=f"finalize:{run_id}:inbox", run_id=run_id,
                expected_revision=state.revision, gateway_epoch=self.gateway_epoch,
                operation_id=operation_id, title=task_title[:120], summary=summary,
                status={TerminalTarget.SUCCEEDED: "completed", TerminalTarget.FAILED: "failed",
                        TerminalTarget.CANCELLED: "cancelled"}[target],
            ))
        else:
            self.state_controller.apply(CompleteFinalizeStepCommand(
                command_id=f"finalize:{run_id}:inbox", run_id=run_id,
                expected_revision=state.revision, gateway_epoch=self.gateway_epoch,
                step_name="inbox", result="not_requested",
            ))
        state = self.state_controller.state(run_id)
        self.state_controller.apply(FinalizeTerminalCommand(
            command_id=f"finalize:{run_id}:terminal", run_id=run_id,
            expected_revision=state.revision, gateway_epoch=self.gateway_epoch,
            reason="FINALIZING 完成",
        ))

    def _code_project(self, session_id: str) -> str:
        owner = getattr(self.code_sessions, "owner", None)
        if callable(owner):
            return str(owner(session_id)[0])
        return "code"

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

    def dream_status(self):
        return self.dream_scheduler.status()

    async def run_dream(self, selected: str | None = None):
        if not self.pool.is_idle():
            raise RuntimeError("普通 Agent 任务仍在运行，Dream 将在任务结束后执行")
        target = date.fromisoformat(selected) if selected else datetime.now().astimezone().date() - timedelta(days=1)
        result = await self._execute_dream_day(target, automatic=False)
        await self._record_dream_result(result, False)
        return result

    async def backfill_dream(self, start: str, end: str):
        if not self.pool.is_idle():
            raise RuntimeError("普通 Agent 任务仍在运行，不能开始 Dream backfill")
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if last < first or (last - first).days >= 31:
            raise ValueError("Dream backfill 日期无效或超过 31 天")
        collected = []
        current = first
        while current <= last:
            collected.append(await self._execute_dream_day(current, automatic=False))
            current = date.fromordinal(current.toordinal() + 1)
        results = tuple(collected)
        for result in results:
            await self._record_dream_result(result, False)
        return results

    async def _execute_dream_day(self, selected: date, *, automatic: bool) -> DreamRunResult:
        run_id = uuid4().hex
        state = self._begin_workload_run(
            run_id=run_id,
            workload=WorkloadKind.DREAM,
            project_id="dream",
            client_id="dream:scheduler" if automatic else "dream:manual",
            task=f"Dream {selected.isoformat()}",
        )
        if not is_runnable(state, None, now=datetime.now().astimezone()):
            raise RuntimeError(f"Dream workload 不可调度：{state.task_state.value}")
        return await self.dream_service.process_day(selected, run_id=run_id)

    def _begin_workload_run(
        self,
        *,
        run_id: str,
        workload: WorkloadKind,
        project_id: str,
        client_id: str,
        task: str,
    ):
        request_hash = hashlib.sha256(task.encode("utf-8")).hexdigest()
        state, duplicate = self.state_controller.create_run(
            run_id=run_id,
            workload_kind=workload,
            project_id=project_id,
            client_id=client_id,
            task=task,
            idempotency_key=f"{workload.value}:{run_id}",
            request_hash=request_hash,
        )
        if duplicate:
            return state
        for target, reason in (
            (TaskState.QUEUED, "维护任务进入队列"),
            (TaskState.STARTING, "维护任务开始初始化"),
            (TaskState.RUNNING, "维护任务开始执行"),
        ):
            state = self.state_controller.apply(TransitionCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.gateway_epoch,
                task_state=target,
                reason=reason,
            )).state
        self.outbox.wake()
        return state

    async def rollback_dream(self, run_id: str | None = None):
        if not self.pool.is_idle():
            raise RuntimeError("普通 Agent 任务仍在运行，不能回滚 Dream")
        result = await self.dream_service.rollback(run_id)
        if result.restored:
            await self.pool.invalidate_profile_context(after_active_turn=True)
            try:
                run = self.store.run(result.run_id)
                state = self.state_controller.state(run.run_id)
                self.state_controller.apply(RecordRuntimeEventCommand(
                    command_id=uuid4().hex,
                    run_id=run.run_id,
                    expected_revision=state.revision,
                    gateway_epoch=self.gateway_epoch,
                    event_type="dream_rolled_back",
                    payload=result.model_dump(mode="json"),
                ))
                self.outbox.wake()
            except KeyError:
                pass
        return result

    async def _record_dream_result(self, result: DreamRunResult, automatic: bool) -> None:
        """把维护运行转换为可重放 Gateway 事件；仅自动任务进入 Inbox。"""
        try:
            state = self.state_controller.state(result.run_id)
        except KeyError:
            state = self._begin_workload_run(
                run_id=result.run_id,
                workload=WorkloadKind.DREAM,
                project_id="dream",
                client_id="dream:scheduler" if automatic else "dream:manual",
                task=f"Dream {result.date}",
            )
        state = self.state_controller.apply(RecordRuntimeEventCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            event_type="dream_started",
            payload={"date": result.date},
            mark_progress=True,
        )).state
        terminal_type = {
            "completed": "dream_completed",
            "noop": "dream_noop",
            "failed": "dream_failed",
        }[result.status]
        target = TerminalTarget.FAILED if result.status == "failed" else TerminalTarget.SUCCEEDED
        state = self.state_controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            task_state=TaskState.FINALIZING,
            terminal_target=target,
            reason="Dream maintenance 进入 FINALIZING",
            error=result.message if target is TerminalTarget.FAILED else None,
            result_summary=result.message if target is TerminalTarget.SUCCEEDED else None,
        )).state
        state = self.state_controller.apply(RecordRuntimeEventCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            event_type=terminal_type,
            payload=result.model_dump(mode="json"),
        )).state
        self._finalize_control_plane(
            state.run_id, target, result.message, task_title=f"Dream {result.date}",
            create_visible_inbox=automatic and result.status != "noop",
        )
        state = self.state_controller.state(state.run_id)
        self.outbox.wake()
        run = self.store.run(state.run_id)
        if result.status == "completed":
            await self.pool.invalidate_profile_context(after_active_turn=True)
        self.outbox.wake()

    def cron_preview(self, schedule: CronSchedule, count: int = 5):
        return self.cron_service.preview(schedule, count=count)

    async def start_run(self, request: RunCreateRequest) -> RunRecord:
        self.store.project(request.project_id)
        request_body = json.dumps(
            {
                "project_id": request.project_id,
                "client_id": request.client_id,
                "task": request.task,
                "session_id": request.session_id,
                "deadline_at": request.deadline_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(request_body.encode("utf-8")).hexdigest()
        state, duplicate = self.state_controller.create_run(
            run_id=uuid4().hex,
            workload_kind=WorkloadKind.CHAT,
            project_id=request.project_id,
            client_id=request.client_id,
            task=request.task,
            session_id=request.session_id,
            idempotency_key=request.idempotency_key or uuid4().hex,
            request_hash=request_hash,
            deadline_at=request.deadline_at,
        )
        run = self.store.run(state.run_id)
        if duplicate:
            return run
        state = self.state_controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            task_state=TaskState.QUEUED,
            reason="Gateway 接收任务",
        )).state
        self.outbox.wake()
        run = self.store.run(state.run_id)
        try:
            await self.pool.submit(run)
        except Exception:
            # Run 已持久化；由 RecoveryCoordinator 或取消/收尾命令继续处理。
            raise
        return run

    def recover_run(self, run_id: str, request: RecoveryDecisionRequest):
        result = self.state_controller.apply(RecoveryDecisionCommand(
            command_id=request.command_id, run_id=run_id,
            expected_revision=request.expected_revision, gateway_epoch=self.gateway_epoch,
            action=request.action, operation_id=request.operation_id,
            actor=request.actor, reason=request.reason,
            observed_result=request.observed_result,
            risk_confirmed=request.risk_confirmed,
        ))
        self.outbox.wake()
        return result

    async def _submit_cron_run(self, job: CronJob, run_id: str) -> None:
        self.store.project(job.project_id)
        request_hash = hashlib.sha256(
            json.dumps(
                {"job_id": job.job_id, "project_id": job.project_id, "prompt": job.prompt},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        state, duplicate = self.state_controller.create_run(
            run_id=run_id,
            workload_kind=WorkloadKind.CRON,
            project_id=job.project_id,
            client_id=f"cron:{job.job_id}",
            task=job.prompt,
            idempotency_key=f"cron:{run_id}",
            request_hash=request_hash,
        )
        run = self.store.run(state.run_id)
        if duplicate:
            return
        state = self.state_controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            task_state=TaskState.QUEUED,
            reason="Cron Scheduler 提交任务",
        )).state
        self.outbox.wake()
        run = self.store.run(state.run_id)
        await self.pool.submit(run)

    async def cancel_run(self, run_id: str) -> bool:
        self.store.run(run_id)
        return await self.pool.cancel(run_id)

    def run_events(self, run_id: str, after_sequence: int = 0):
        events = self.state_controller.events(run_id, after_sequence)
        return events if events else self.store.read_events(run_id, after_sequence)

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> bool:
        approval = self.state_controller.approval(approval_id)
        result = await self.pool.approvals.decide(
            approval_id,
            decision.client_id,
            decision.approved,
        )
        if not self.pool.has_active_run(approval.run_id):
            await self.recovery.recover()
        return result

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
            self.config.coding_source_root,
        )

    async def manage_skill(self, request: SkillManageRequest):
        project = self.store.project(request.project_id)

        async def approve(name: str, arguments: dict) -> bool:
            del name, arguments
            return request.confirmed

        service = SkillService(
            self.config.agent_root,
            Path(project.path),
            self.config.coding_source_root,
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
