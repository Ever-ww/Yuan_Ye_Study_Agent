"""Gateway 业务编排入口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from Agent import ExtensionLoader, RuntimeConfig
from Agent.state import (
    CompleteOperationAttemptCommand,
    CreateOperationWithAttemptCommand,
    FailOperationAttemptCommand,
    MarkOperationAttemptUnknownCommand,
    OperationFailureKind,
    OperationKind,
    PersistenceContract,
    RecordRuntimeEventCommand,
    ReconcileOperationAttemptCommand,
    ReconcileResult,
    ReconcileStatus,
    RecoveryDecisionCommand,
    RetryPolicySnapshot,
    StartOperationAttemptCommand,
    TaskState,
    TerminalTarget,
    ToolIdempotency,
    TransitionCommand,
    WorkloadKind,
    is_runnable,
)
from cron import (
    CronDispatch,
    CronJob,
    CronJobCreateRequest,
    CronJobEditRequest,
    CronPaperResearchPresetRequest,
    CronSchedule,
    CronScheduleCalculator,
    CronScheduler,
    CronService,
    CronStore,
)
from gateway.code_sessions import CodeSessionManager
from gateway.audit import AuditSanitizer
from gateway.events import GatewayEventBus
from gateway.outbox import OutboxDispatcher
from gateway.recovery import RecoveryCoordinator
from gateway.state_controller import StateController
from gateway.models import (
    ApprovalDecision,
    CodeSessionCreateRequest,
    CodeTurnRequest,
    HarnessEvolutionDecision,
    HarnessDreamDecisionRequest,
    HarnessDreamFreezeRequest,
    HarnessDreamRunRequest,
    HarnessDreamRevertRequest,
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
from gateway.harness_dream import (
    HarnessDreamChangeSet,
    HarnessDreamRunResult,
    HarnessDreamStatus,
    HarnessRevertProposal,
)
from gateway.restart import GatewayRestartCoordinator
from backup import (
    AgentHomeMaintenanceCoordinator,
    AgentHomeWriteGate,
    BackupScheduler,
    BackupService,
    SensitiveEnvSanitizer,
    assert_restore_inactive,
)
from sandbox import CheckpointDreamCoordinator


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
            database_path=self.store.database_path,
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
        from gateway.harness_evolution import GatewayHarnessEvolutionService
        self.harness_evolution = GatewayHarnessEvolutionService(
            config,
            store=self.store,
            state_controller=self.state_controller,
        )
        self._harness_dream_tick_lock = asyncio.Lock()
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
            harness_evolution_service=self.harness_evolution,
            cron_tool_authorizer=self.cron_service.tool_preapproved,
            cron_terminal_callback=self._settle_cron_terminal,
        )
        self.recovery = RecoveryCoordinator(
            self.state_controller,
            self.store,
            self.pool.submit,
            self.pool.finalizer,
        )
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            self._submit_cron_dispatch,
            gateway_epoch=lambda: self.gateway_epoch,
            write_gate=self.write_gate,
        )
        self.cron_service.set_waker(self.cron_scheduler.wake)
        self.dream_service = DreamService(
            config,
            excluded_sessions=self.store.automated_session_ids,
        )
        self.checkpoint_dream = CheckpointDreamCoordinator(
            config.workspace_root,
            config.agent_root,
            checkpoint_limit=config.sandbox_checkpoint_limit,
            merged_ref_retention_days=config.sandbox_checkpoint_merged_branch_retention_days,
            model_runner=self.dream_service.run_stateless_model,
        )
        self.dream_scheduler = DreamScheduler(
            self.dream_service,
            self.pool.is_idle,
            self._record_dream_result,
            heartbeat_seconds=config.cron_heartbeat_seconds,
            run_day=lambda selected: self._execute_dream_day(selected, automatic=True),
            run_checkpoint_day=self.checkpoint_dream.process_due,
            run_harness_day=lambda selected: self.run_harness_dream(
                selected.isoformat(), automatic=True, actor="dream:scheduler",
            ),
            write_gate=self.write_gate,
        )
        self._browser_codes: dict[str, float] = {}
        self.code_sessions = CodeSessionManager(config)
        from tools import HarnessDreamTool, HarnessErrorTool, HarnessManualTool
        self.harness_manual_tool = HarnessManualTool(lambda: self.code_sessions)
        self.harness_error_tool = HarnessErrorTool(self.harness_evolution)
        self.harness_dream_tool = HarnessDreamTool(self)
        self.maintenance.register("runtime_pool", self.pool)
        self.maintenance.register("cron", self.cron_scheduler)
        self.maintenance.register("dream", self.dream_scheduler)
        self.maintenance.register("outbox", self.outbox)
        self.maintenance.register("reference_embedding", self.reference_embedding_worker)
        self.maintenance.register("harness", self.code_sessions)
        self.restart_coordinator = GatewayRestartCoordinator(
            agent_root=config.agent_root, source_root=source_root,
            port=config.gateway_port, gateway_epoch=self.gateway_epoch,
            state_controller=self.state_controller, maintenance=self.maintenance,
            is_idle=self.pool.is_idle,
            timeout_seconds=config.harness_dream_restart_wait_timeout_seconds,
        )
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
            await self.cron_store.ensure()
            await self._reconcile_cron_dispatches()
            await self.recovery.recover()
            await self._recover_harness_dream_generations()
            await self.restart_coordinator.recover_pending()
            await self.cron_scheduler.start()
            await self.dream_scheduler.start()
            await self.backup_scheduler.start()
        except Exception:
            await self.outbox.close()
            await self.reference_embedding_worker.close()
            raise

    async def _reconcile_cron_dispatches(self) -> None:
        """Complete durable Cron bindings/projections; never create a second Run."""
        for dispatch in await self.cron_store.active_dispatches():
            if dispatch.status == "claimed":
                # Deterministic run identity makes a crashed bind safely resumable.
                await self._submit_cron_dispatch(dispatch)
                continue
            if not dispatch.run_id:
                continue
            try:
                run = self.store.run(dispatch.run_id)
            except KeyError:
                await self.cron_store.mark_recovery_required(dispatch.dispatch_id, "bound cron run missing")
                continue
            if run.status in {"completed", "failed", "cancelled", "interrupted"}:
                await self._settle_cron_terminal(run)

    async def create_backup(self, passphrase: str, output: Path | None = None):
        return await self.backup_service.create(
            passphrase=passphrase,
            output=output,
            kind="manual",
            drain_timeout_seconds=self.config.backup_drain_timeout_seconds,
        )

    async def _recover_harness_dream_generations(self) -> None:
        """Reconcile active DREAM generations without ever replaying the Engine."""
        for row in self.state_controller.active_harness_dream_changesets():
            stable_key = str(row["stable_key"])
            run_id = str(row["active_run_id"])
            try:
                state = self.state_controller.state(run_id)
            except KeyError:
                self.state_controller.release_orphan_harness_dream_generation(
                    stable_key, expected_revision=int(row["revision"]), run_id=run_id,
                )
                continue
            changeset = HarnessDreamChangeSet.model_validate_json(
                str(row["changeset_json"]), strict=True,
            )
            operations = self.state_controller.operations(run_id)
            operation = next(
                (item for item in reversed(operations) if item.name == "harness_dream"), None,
            )
            if operation is None:
                evidence = {"status": "UNKNOWN", "evidence": "missing Dream operation"}
            elif operation.status.value == "completed" and operation.result:
                try:
                    evidence = json.loads(operation.result)
                except json.JSONDecodeError:
                    evidence = {"status": "UNKNOWN", "evidence": "invalid operation result"}
            else:
                evidence = await self.harness_evolution.reconcile_dream(
                    changeset, generation=int(row["generation"]),
                )
                if operation is not None:
                    attempt = self.state_controller.current_attempt(operation.operation_id)
                    status = ReconcileStatus(str(evidence.get("status") or "UNKNOWN").lower())
                    observed = json.dumps(
                        evidence.get("evidence"), ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"),
                    )
                    state = self.state_controller.state(run_id)
                    if attempt.status.value in {"running", "unknown"}:
                        self.state_controller.apply(ReconcileOperationAttemptCommand(
                            command_id=hashlib.sha256(
                                f"dream-reconcile:{attempt.attempt_id}:{status.value}".encode(),
                            ).hexdigest(),
                            run_id=run_id, expected_revision=state.revision,
                            gateway_epoch=self.gateway_epoch, attempt_id=attempt.attempt_id,
                            result=ReconcileResult(
                                status=status, evidence=observed,
                                result_source="harness_dream_reconcile",
                                observed_result=(
                                    observed if status is ReconcileStatus.COMPLETED else None
                                ),
                                checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                            ),
                        ))
            reconciled = str(evidence.get("status") or "").upper()
            if reconciled == "COMPLETED" or str(evidence.get("status")) == "merged":
                raw_result = {
                    "status": "merged", "message": "Recovered committed Harness Dream merge",
                    "invocation_id": str((evidence.get("evidence") or {}).get("invocation_id", ""))
                    if isinstance(evidence.get("evidence"), dict) else "",
                    "merged_commit": str((evidence.get("evidence") or {}).get("merged_commit", ""))
                    if isinstance(evidence.get("evidence"), dict) else "",
                    "restart_required": True, "run_id": run_id,
                }
                outcome = "success"
            elif reconciled == "NOT_APPLIED":
                raw_result = {
                    "status": "confirmed_failed", "message": "Dream Engine was not applied",
                    "reconcile": evidence, "run_id": run_id,
                }
                outcome = "failed"
            else:
                raw_result = {
                    "status": "unknown", "message": "Harness Dream requires recovery",
                    "reconcile": evidence, "run_id": run_id,
                }
                outcome = "unknown"
            current = self.state_controller.harness_dream_changeset(stable_key=stable_key)
            assert current is not None
            self.state_controller.finish_harness_dream_generation(
                stable_key, run_id=run_id, expected_revision=int(current["revision"]),
                status=outcome, result=raw_result,
            )
            self.outbox.wake()
            if outcome == "unknown":
                current_state = self.state_controller.state(run_id)
                if current_state.task_state is not TaskState.RECOVERY_REQUIRED:
                    self.state_controller.apply(TransitionCommand(
                        command_id=uuid4().hex, run_id=run_id,
                        expected_revision=current_state.revision,
                        gateway_epoch=self.gateway_epoch,
                        task_state=TaskState.RECOVERY_REQUIRED,
                        reason="Recovered Harness Dream has unknown Git effect",
                    ))
            else:
                await self._finish_workload(
                    run_id,
                    TerminalTarget.SUCCEEDED if outcome == "success" else TerminalTarget.FAILED,
                    str(raw_result["message"]),
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
        await self._finalize_control_plane(run_id)
        self.outbox.wake()

    async def close(self) -> None:
        try:
            await self.restart_coordinator.close()
            await self.backup_scheduler.close()
            await self.dream_scheduler.close()
            await self.cron_scheduler.close()
            await self.code_sessions.close()
            await self.pool.close()
        finally:
            await self.reference_embedding_worker.close()
            await self.outbox.close()

    async def start_code_session(self, request: CodeSessionCreateRequest):
        project = self.store.project(request.project_id)
        origin_session_id = request.origin_session_id
        if origin_session_id is None:
            memory = MemoryStore(
                self.config.memory_dir,
                workspace_root=Path(project.path),
                agent_root=self.config.agent_root,
            )
            origin_session_id = memory.create_session("")
        return await self._run_code_workload(
            WorkloadKind.CODE_SESSION_START,
            request.project_id,
            request.client_id,
            "启动 Coding Session",
            lambda run_id: self.harness_manual_tool.start(
                request.project_id, request.client_id,
                origin_session_id=origin_session_id,
                origin_run_id=self._latest_origin_run_id(
                    request.project_id, origin_session_id, fallback=run_id,
                ),
                origin_context=self._code_origin_context(
                    request.project_id, origin_session_id,
                    self._latest_origin_run_id(
                        request.project_id, origin_session_id, fallback=run_id,
                    ),
                ),
            ),
        )

    async def run_code_turn(self, session_id: str, request: CodeTurnRequest):
        project_id = self._code_project(session_id)
        return await self._run_code_workload(
            WorkloadKind.CODE_TURN,
            project_id,
            request.client_id,
            request.task,
            lambda _run_id: self.harness_manual_tool.turn(session_id, request.client_id, request.task),
        )

    async def finalize_code_session(self, session_id: str, client_id: str):
        project_id = self._code_project(session_id)
        return await self._run_code_workload(
            WorkloadKind.CODE_FINALIZE,
            project_id,
            client_id,
            "合并并结束 Coding Session",
            lambda _run_id: self.harness_manual_tool.finalize(session_id, client_id),
        )

    async def abort_code_session(self, session_id: str, client_id: str):
        project_id = self._code_project(session_id)
        return await self._run_code_workload(
            WorkloadKind.CODE_ABORT,
            project_id,
            client_id,
            "放弃 Coding Session",
            lambda _run_id: self.harness_manual_tool.abort(session_id, client_id),
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
            result = await operation(state.run_id)
        except Exception as exc:
            await self._finish_workload(state.run_id, TerminalTarget.FAILED, str(exc) or type(exc).__name__)
            raise
        await self._finish_workload(state.run_id, TerminalTarget.SUCCEEDED, "Coding workload 完成")
        return result

    def _latest_origin_run_id(
        self, project_id: str, session_id: str, *, fallback: str,
    ) -> str:
        candidates = [
            run for run in self.store.list_runs(project_id)
            if run.session_id == session_id and run.workload_kind == WorkloadKind.CHAT.value
        ]
        # GatewayStore orders newest first.
        return candidates[0].run_id if candidates else fallback

    def _code_origin_context(
        self, project_id: str, session_id: str, origin_run_id: str,
    ) -> dict[str, object]:
        project = self.store.project(project_id)
        memory = MemoryStore(
            self.config.memory_dir, workspace_root=Path(project.path),
            agent_root=self.config.agent_root,
        )
        records = list(memory.session_records(session_id)) if memory.has_session(session_id) else []
        canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "origin_project_id": project_id,
            "origin_session_id": session_id,
            "origin_run_id": origin_run_id,
            "session_record_ids": tuple(
                str(record["record_id"]) for record in records if record.get("record_id")
            ),
            "session_records_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "context_summary": str(AuditSanitizer.sanitize("\n".join(
                f"{record.get('role')}: {str(record.get('content') or '')[:1000]}"
                for record in records[-8:] if record.get("role") in {"user", "assistant"}
            )[-6000:])),
            "trigger_evidence": {"entry": "/code"},
        }

    async def decide_harness_evolution(
        self, proposal_id: str, decision: HarnessEvolutionDecision,
    ) -> dict[str, object]:
        proposal = self.harness_evolution.decide_proposal(
            proposal_id, confirmed=decision.confirmed, client_id=decision.client_id,
        )
        if not decision.confirmed:
            return proposal
        result = await self._run_code_workload(
            WorkloadKind.HARNESS_EVOLUTION,
            str(proposal["origin_project_id"]), decision.client_id,
            f"ERROR Harness Evolution: {proposal['task']}",
            lambda _run_id: self.harness_error_tool.run_proposal(proposal_id),
        )
        return {**proposal, "result": result}

    async def _finish_workload(self, run_id: str, target: TerminalTarget, summary: str) -> None:
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
        await self._finalize_control_plane(run_id)
        self.outbox.wake()

    async def _finalize_control_plane(
        self,
        run_id: str,
    ) -> None:
        await self.pool.finalizer.finalize(run_id)

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

    async def initialize_paper_research_cron(
        self,
        request: CronPaperResearchPresetRequest,
    ) -> CronJob:
        self.store.project(request.project_id)
        result = await self.cron_service.ensure_paper_research_preset(
            project_id=request.project_id,
            expression=request.expression,
            timezone_name=request.timezone,
        )
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

    async def retry_cron_dispatch(self, dispatch_id: str) -> CronDispatch:
        result = await self.cron_service.retry(dispatch_id)
        self.cron_scheduler.wake()
        return result

    async def cron_history(self, job_id: str, *, limit: int = 100) -> tuple[CronDispatch, ...]:
        return await self.cron_service.history(job_id, limit=limit)

    async def remove_cron(self, job_id: str) -> CronJob:
        result = await self.cron_service.remove(job_id)
        self.cron_scheduler.wake()
        return result

    async def cron_status(self):
        return await self.cron_service.status(error=self.cron_scheduler.last_error)

    def dream_status(self):
        return self.dream_scheduler.status()

    def harness_dream_status(self) -> HarnessDreamStatus:
        scanner = self.harness_evolution.dream_scanner
        today = datetime.now(scanner.zone).date().isoformat()
        freeze = self.state_controller.harness_dream_freeze(scanner.source_identity, today)
        latest = self.state_controller.latest_harness_dream_changeset(scanner.source_identity)
        return HarnessDreamStatus(
            enabled=self.config.harness_dream_enabled,
            frozen=freeze is not None,
            freeze=freeze,
            latest=latest,
        )

    def freeze_harness_dream(self, *, actor: str, reason: str) -> dict[str, object]:
        scanner = self.harness_evolution.dream_scanner
        selected = datetime.now(scanner.zone).date().isoformat()
        return self.state_controller.freeze_harness_dream(
            scanner.source_identity, selected, actor=actor, reason=reason,
        )

    def unfreeze_harness_dream(self) -> dict[str, object]:
        scanner = self.harness_evolution.dream_scanner
        selected = datetime.now(scanner.zone).date().isoformat()
        return {
            "removed": self.state_controller.unfreeze_harness_dream(
                scanner.source_identity, selected,
            ),
            "date": selected,
        }

    async def run_harness_dream(
        self,
        selected: str | None,
        *,
        automatic: bool,
        actor: str,
        allow_blocked: bool = False,
        expected_revision: int | None = None,
    ) -> HarnessDreamRunResult:
        async with self._harness_dream_tick_lock:
            scanner = self.harness_evolution.dream_scanner
            now = datetime.now(scanner.zone)
            if automatic and not self.config.harness_dream_enabled:
                return HarnessDreamRunResult(
                    status="no_changes", date=(now.date() - timedelta(days=1)).isoformat(),
                    message="Harness Dream is disabled",
                )
            row: dict[str, object] | None = None
            selected_date: date
            if selected:
                try:
                    selected_date = date.fromisoformat(selected)
                except ValueError:
                    row = self.state_controller.harness_dream_changeset_for_run(selected)
                    if row is None:
                        row = self.state_controller.harness_dream_changeset(stable_key=selected)
                    if row is None:
                        raise KeyError(f"Unknown Harness Dream date or operation: {selected}")
                    selected_date = date.fromisoformat(str(row["dream_date"]))
            else:
                selected_date = now.date() - timedelta(days=1)
            if automatic:
                freeze = self.state_controller.harness_dream_freeze(
                    scanner.source_identity, now.date().isoformat(),
                )
                if freeze is not None:
                    return HarnessDreamRunResult(
                        status="frozen", date=selected_date.isoformat(),
                        message=f"Harness Dream auto-run is frozen: {freeze['reason']}",
                    )
            if row is None:
                row = self.state_controller.harness_dream_changeset(
                    dream_date=selected_date.isoformat(), source_identity=scanner.source_identity,
                )
            if row is None:
                changeset = self.harness_evolution.discover_dream_changes(
                    selected_date, cutoff_at=now,
                )
                row, duplicate = self.state_controller.claim_harness_dream(
                    changeset.model_dump(mode="json"),
                    automatic_cycle=automatic,
                    no_changes=not changeset.evidence,
                )
                if not changeset.evidence:
                    return HarnessDreamRunResult(
                        status="no_changes", date=changeset.date,
                        stable_key=changeset.stable_key,
                        message="No eligible Harness merge was committed before the Dream cutoff",
                    )
                if duplicate:
                    changeset = HarnessDreamChangeSet.model_validate_json(
                        str(row["changeset_json"]), strict=True,
                    )
            else:
                changeset = HarnessDreamChangeSet.model_validate_json(
                    str(row["changeset_json"]), strict=True,
                )
            current_status = str(row["status"])
            if current_status in {
                "success", "no_changes", "running", "unknown", "restart_wait_timeout",
            }:
                return self._harness_dream_existing_result(row)
            if automatic and current_status != "discovered":
                return self._harness_dream_existing_result(row)
            if current_status == "blocked" and not allow_blocked:
                return self._harness_dream_existing_result(row)
            selected_revision = int(row["revision"])
            if expected_revision is not None and selected_revision != expected_revision:
                from gateway.state_controller import StateConflictError
                raise StateConflictError("Harness Dream decision revision conflict")
            run_id = uuid4().hex
            locked = self.state_controller.start_harness_dream_generation(
                changeset.stable_key,
                run_id=run_id,
                expected_revision=selected_revision,
                allow_blocked=allow_blocked,
            )
            generation = int(locked["generation"])
            state = self._begin_workload_run(
                run_id=run_id,
                workload=WorkloadKind.HARNESS_DREAM,
                project_id="harness-dream",
                client_id=actor,
                task=f"Harness Dream {changeset.date} generation {generation}",
            )
            operation_id = hashlib.sha256(
                f"{changeset.stable_key}:g{generation}:operation".encode("utf-8"),
            ).hexdigest()
            attempt_id = hashlib.sha256(f"{operation_id}:attempt:1".encode("utf-8")).hexdigest()
            state = self.state_controller.apply(CreateOperationWithAttemptCommand(
                command_id=hashlib.sha256(f"{operation_id}:create".encode()).hexdigest(),
                run_id=run_id, expected_revision=state.revision,
                gateway_epoch=self.gateway_epoch,
                operation_id=operation_id, attempt_id=attempt_id,
                turn_id=state.turn_id, kind=OperationKind.TOOL,
                name="harness_dream",
                stable_key=f"{changeset.stable_key}:g{generation}",
                request_hash=changeset.changeset_hash,
                idempotency=ToolIdempotency.NON_IDEMPOTENT,
                side_effecting=True,
                external_idempotency_key=changeset.stable_key,
                retry_policy_snapshot=RetryPolicySnapshot(
                    max_attempts=1, base_seconds=0.0, max_seconds=0.0,
                    automatic=False, requires_reconcile=True,
                    requires_human_confirmation=not automatic,
                ),
            )).state
            state = self.state_controller.apply(StartOperationAttemptCommand(
                command_id=hashlib.sha256(f"{attempt_id}:start".encode()).hexdigest(),
                run_id=run_id, expected_revision=state.revision,
                gateway_epoch=self.gateway_epoch, attempt_id=attempt_id,
            )).state
            try:
                raw_result = await self.harness_evolution.execute_dream(
                    changeset, generation=generation, automatic=automatic, run_id=run_id,
                )
                outcome = self._map_harness_dream_outcome(raw_result)
                canonical = json.dumps(
                    raw_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                state = self.state_controller.state(run_id)
                if outcome == "unknown":
                    state = self.state_controller.apply(MarkOperationAttemptUnknownCommand(
                        command_id=hashlib.sha256(f"{attempt_id}:unknown".encode()).hexdigest(),
                        run_id=run_id, expected_revision=state.revision,
                        gateway_epoch=self.gateway_epoch, attempt_id=attempt_id,
                        failure_reason="Harness Dream Git effect could not be proven",
                    )).state
                elif outcome == "failed":
                    state = self.state_controller.apply(FailOperationAttemptCommand(
                        command_id=hashlib.sha256(f"{attempt_id}:failed".encode()).hexdigest(),
                        run_id=run_id, expected_revision=state.revision,
                        gateway_epoch=self.gateway_epoch, attempt_id=attempt_id,
                        failure_kind=OperationFailureKind.TERMINAL,
                        failure_reason=str(raw_result.get("message") or "Harness Dream failed"),
                    )).state
                else:
                    state = self.state_controller.apply(CompleteOperationAttemptCommand(
                        command_id=hashlib.sha256(f"{attempt_id}:complete".encode()).hexdigest(),
                        run_id=run_id, expected_revision=state.revision,
                        gateway_epoch=self.gateway_epoch, attempt_id=attempt_id,
                        result=canonical,
                        result_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                        result_source="harness_dream_engine",
                    )).state
            except Exception as exc:
                raw_result = {
                    "status": "unknown", "message": str(exc) or type(exc).__name__,
                    "invocation_id": hashlib.sha256(
                        f"dream:{changeset.stable_key}:g{generation}".encode("utf-8"),
                    ).hexdigest()[:32],
                }
                outcome = "unknown"
                state = self.state_controller.state(run_id)
                self.state_controller.apply(MarkOperationAttemptUnknownCommand(
                    command_id=hashlib.sha256(f"{attempt_id}:exception-unknown".encode()).hexdigest(),
                    run_id=run_id, expected_revision=state.revision,
                    gateway_epoch=self.gateway_epoch, attempt_id=attempt_id,
                    failure_reason=str(exc) or type(exc).__name__,
                ))
            raw_result["run_id"] = run_id
            current_changeset = self.state_controller.harness_dream_changeset(
                stable_key=changeset.stable_key,
            )
            assert current_changeset is not None
            committed = self.state_controller.finish_harness_dream_generation(
                changeset.stable_key,
                run_id=run_id,
                expected_revision=int(current_changeset["revision"]),
                status=outcome,
                result=raw_result,
            )
            self.outbox.wake()
            if outcome == "unknown":
                state = self.state_controller.state(run_id)
                self.state_controller.apply(TransitionCommand(
                    command_id=uuid4().hex, run_id=run_id,
                    expected_revision=state.revision, gateway_epoch=self.gateway_epoch,
                    task_state=TaskState.RECOVERY_REQUIRED,
                    reason="Harness Dream merge effect requires reconciliation",
                ))
            else:
                target = TerminalTarget.FAILED if outcome == "failed" else TerminalTarget.SUCCEEDED
                await self._finish_workload(
                    run_id, target, str(raw_result.get("message") or f"Harness Dream {outcome}"),
                )
            result = HarnessDreamRunResult(
                status=outcome,
                date=changeset.date,
                stable_key=changeset.stable_key,
                generation=int(committed["generation"]),
                run_id=run_id,
                message=str(raw_result.get("message") or f"Harness Dream {outcome}"),
                invocation_id=str(raw_result.get("invocation_id") or ""),
                merged_commit=str(raw_result.get("merged_commit") or ""),
                changed_files=tuple(raw_result.get("changed_files") or ()),
                restart_required=bool(raw_result.get("restart_required")),
            )
            if (
                outcome == "success" and result.restart_required
                and self.config.harness_dream_auto_restart
            ):
                await self._request_harness_dream_restart(result)
            return result

    @staticmethod
    def _map_harness_dream_outcome(result: dict[str, object]) -> str:
        status = str(result.get("status") or "")
        if status in {"merged", "no_code_changes"}:
            return "success"
        if status == "deferred":
            return "deferred"
        if status in {"blocked_main_changed", "requires_broader_source_change"}:
            return "blocked"
        if status == "unknown":
            return "unknown"
        return "failed"

    @staticmethod
    def _harness_dream_existing_result(row: dict[str, object]) -> HarnessDreamRunResult:
        raw = json.loads(str(row.get("result_json") or "{}"))
        status = str(row["status"])
        if status == "running":
            status = "blocked"
        return HarnessDreamRunResult(
            status=status,
            date=str(row["dream_date"]),
            stable_key=str(row["stable_key"]),
            generation=int(row["generation"]),
            run_id=str(row.get("active_run_id") or raw.get("run_id") or ""),
            message=str(raw.get("message") or f"Harness Dream is {row['status']}"),
            invocation_id=str(raw.get("invocation_id") or ""),
            merged_commit=str(raw.get("merged_commit") or ""),
            changed_files=tuple(raw.get("changed_files") or ()),
            restart_required=bool(raw.get("restart_required")),
        )

    async def decide_harness_dream(
        self, operation_id: str, request: HarnessDreamDecisionRequest,
    ) -> HarnessDreamRunResult | dict[str, object]:
        row = self.state_controller.harness_dream_changeset_for_run(operation_id)
        if row is None:
            raise KeyError(operation_id)
        if int(row["revision"]) != request.expected_revision:
            from gateway.state_controller import StateConflictError
            raise StateConflictError("Harness Dream decision revision conflict")
        if str(row["status"]) == "unknown":
            if not request.approved:
                return {"status": "unknown", "approved": False, "reason": request.reason}
            changeset = HarnessDreamChangeSet.model_validate_json(
                str(row["changeset_json"]), strict=True,
            )
            reconciled = await self.harness_evolution.reconcile_dream(
                changeset, generation=int(row["generation"]),
            )
            reconcile_status = str(reconciled.get("status") or "UNKNOWN").upper()
            if reconcile_status == "UNKNOWN":
                return {"status": "unknown", "approved": True, "reconcile": reconciled}
            run_id = str(row["active_run_id"])
            operation = next(
                item for item in reversed(self.state_controller.operations(run_id))
                if item.name == "harness_dream"
            )
            attempt = self.state_controller.current_attempt(operation.operation_id)
            state = self.state_controller.state(run_id)
            observed = json.dumps(
                reconciled.get("evidence"), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            if attempt.status.value in {"running", "unknown"}:
                self.state_controller.apply(ReconcileOperationAttemptCommand(
                    command_id=hashlib.sha256(
                        f"dream-decision:{attempt.attempt_id}:{reconcile_status}".encode(),
                    ).hexdigest(),
                    run_id=run_id, expected_revision=state.revision,
                    gateway_epoch=self.gateway_epoch, attempt_id=attempt.attempt_id,
                    result=ReconcileResult(
                        status=ReconcileStatus(reconcile_status.lower()),
                        evidence=observed, result_source="harness_dream_reconcile",
                        observed_result=(
                            observed if reconcile_status == "COMPLETED" else None
                        ),
                        checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    ),
                ))
            outcome = "success" if reconcile_status == "COMPLETED" else "failed"
            raw_result = {
                "status": "merged" if outcome == "success" else "confirmed_failed",
                "message": f"Harness Dream reconcile: {reconcile_status}",
                "reconcile": reconciled, "run_id": run_id,
                "merged_commit": (
                    str(reconciled.get("evidence", {}).get("merged_commit", ""))
                    if isinstance(reconciled.get("evidence"), dict) else ""
                ),
                "restart_required": outcome == "success",
            }
            current = self.state_controller.harness_dream_changeset(
                stable_key=str(row["stable_key"]),
            )
            assert current is not None
            committed = self.state_controller.finish_harness_dream_generation(
                str(row["stable_key"]), run_id=run_id,
                expected_revision=int(current["revision"]), status=outcome,
                result=raw_result,
            )
            await self._finish_workload(
                run_id,
                TerminalTarget.SUCCEEDED if outcome == "success" else TerminalTarget.FAILED,
                str(raw_result["message"]),
            )
            result = self._harness_dream_existing_result(committed)
            if outcome == "success" and result.restart_required:
                await self._request_harness_dream_restart(result)
            return result
        if str(row["status"]) != "blocked":
            raise RuntimeError("Only a BLOCKED or UNKNOWN Harness Dream can be decided")
        if not request.approved:
            return {"status": "blocked", "approved": False, "reason": request.reason}
        return await self.run_harness_dream(
            operation_id, automatic=False, actor=request.client_id,
            allow_blocked=True, expected_revision=request.expected_revision,
        )

    async def _request_harness_dream_restart(self, result: HarnessDreamRunResult) -> None:
        if not result.stable_key or not result.merged_commit:
            raise RuntimeError("Harness Dream restart requires stable_key and merged_commit")
        await self.restart_coordinator.request(
            stable_key=result.stable_key, expected_commit=result.merged_commit,
            run_id=result.run_id,
        )

    async def create_harness_dream_revert(
        self, operation_id: str, *, actor: str,
    ) -> dict[str, object]:
        del actor  # actor is carried by the authenticated Gateway request/audit boundary.
        row = self.state_controller.harness_dream_changeset_for_run(operation_id)
        if row is None:
            row = self.state_controller.harness_dream_changeset(stable_key=operation_id)
        if row is None or str(row["status"]) != "success":
            raise RuntimeError("Only a successfully merged Harness Dream can be reverted")
        result = json.loads(str(row.get("result_json") or "{}"))
        merged_commit = str(result.get("merged_commit") or "")
        if not merged_commit:
            raise RuntimeError("Harness Dream merge receipt has no merged commit")
        proposal_id = hashlib.sha256(
            f"dream-revert:{row['stable_key']}:{merged_commit}".encode("utf-8"),
        ).hexdigest()[:32]
        existing = self.state_controller.harness_dream_revert(proposal_id)
        if existing is not None:
            return {**existing, "proposal": json.loads(str(existing["proposal_json"]))}
        source_identity = self.harness_evolution.dream_scanner.source_identity
        worktree = (
            self.config.agent_root / ".yy" / "harness-evolution" / "reverts"
            / source_identity / proposal_id
        )
        placeholder = HarnessRevertProposal(
            proposal_id=proposal_id, stable_key=str(row["stable_key"]),
            operation_run_id=operation_id, source_identity=source_identity,
            merged_commit=merged_commit, base_head="", candidate_commit="",
            candidate_branch=f"harness-dream-revert/{proposal_id[:12]}",
            worktree_path=str(worktree), status="proposed",
            validation_summary=("candidate preparation pending",),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        claimed = self.state_controller.create_harness_dream_revert(
            proposal_id, str(row["stable_key"]), operation_id,
            placeholder.model_dump(mode="json"),
        )
        try:
            proposal = await asyncio.to_thread(
                self._build_harness_dream_revert,
                proposal_id, str(row["stable_key"]), operation_id, merged_commit,
            )
        except Exception as exc:
            proposal = placeholder.model_copy(update={
                "status": "blocked",
                "validation_summary": (
                    f"candidate preparation failed: {str(exc) or type(exc).__name__}",
                ),
            })
        stored = self.state_controller.decide_harness_dream_revert(
            proposal_id, expected_revision=int(claimed["revision"]),
            status=proposal.status, proposal=proposal.model_dump(mode="json"),
        )
        self.outbox.wake()
        return {**stored, "proposal": proposal.model_dump(mode="json")}

    def _build_harness_dream_revert(
        self, proposal_id: str, stable_key: str, operation_id: str, merged_commit: str,
    ) -> HarnessRevertProposal:
        source_root = (self.config.coding_source_root or Path(__file__).resolve().parents[1]).resolve()
        env = SensitiveEnvSanitizer.subprocess_env({"GIT_TERMINAL_PROMPT": "0"})

        def git(*arguments: str, cwd: Path = source_root, check: bool = True) -> str:
            completed = subprocess.run(
                ["git", *arguments], cwd=cwd, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=300,
            )
            if check and completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or f"git {' '.join(arguments)} failed")
            return completed.stdout.decode("utf-8", errors="replace").strip()

        if git("status", "--porcelain"):
            raise RuntimeError("Source repository is dirty; revert candidate was deferred")
        base_head = git("rev-parse", "HEAD")
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", merged_commit, base_head],
            cwd=source_root, env=env, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        ).returncode != 0:
            raise RuntimeError("Merged Dream commit is not an ancestor of current HEAD")
        source_identity = self.harness_evolution.dream_scanner.source_identity
        worktree = (
            self.config.agent_root / ".yy" / "harness-evolution" / "reverts"
            / source_identity / proposal_id
        )
        branch = f"harness-dream-revert/{proposal_id[:12]}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-b", branch, str(worktree), base_head)
        status = "proposed"
        validations: list[str] = []
        candidate_commit = ""
        try:
            revert = subprocess.run(
                ["git", "revert", "--no-commit", merged_commit], cwd=worktree,
                env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=300,
            )
            if revert.returncode != 0:
                status = "blocked"
                validations.append("git revert conflict")
            else:
                commands = (
                    ([sys.executable, "-m", "compileall", "-q", "Agent", "gateway", "tool", "tools"], "compileall"),
                    ([sys.executable, "-m", "pytest", "-q"], "pytest"),
                    ([sys.executable, "-m", "unittest", "-q"], "unittest"),
                    (["uv", "lock", "--check"], "uv lock --check"),
                    (["git", "diff", "--check"], "git diff --check"),
                )
                for command, label in commands:
                    completed = subprocess.run(
                        command, cwd=worktree, env=env, check=False,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
                    )
                    validations.append(f"{label}: {completed.returncode}")
                    if completed.returncode != 0:
                        status = "blocked"
                        break
                if status == "proposed":
                    git("add", "-A", cwd=worktree)
                    git("commit", "-m", f"revert: Harness Dream {merged_commit[:12]}", cwd=worktree)
                    candidate_commit = git("rev-parse", "HEAD", cwd=worktree)
        except Exception:
            # Preserve the worktree and Git evidence for operator inspection.
            raise
        return HarnessRevertProposal(
            proposal_id=proposal_id, stable_key=stable_key,
            operation_run_id=operation_id, source_identity=source_identity,
            merged_commit=merged_commit, base_head=base_head,
            candidate_commit=candidate_commit, candidate_branch=branch,
            worktree_path=str(worktree), status=status,
            validation_summary=tuple(validations),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    async def decide_harness_dream_revert(
        self, proposal_id: str, request: HarnessDreamDecisionRequest,
    ) -> dict[str, object]:
        row = self.state_controller.harness_dream_revert(proposal_id)
        if row is None:
            raise KeyError(proposal_id)
        if int(row["revision"]) != request.expected_revision:
            from gateway.state_controller import StateConflictError
            raise StateConflictError("Harness Dream revert decision revision conflict")
        if str(row["status"]) not in {"proposed", "blocked", "approved"}:
            raise RuntimeError("Harness Dream revert proposal is already decided")
        proposal = HarnessRevertProposal.model_validate_json(
            str(row["proposal_json"]), strict=True,
        )
        if not request.approved:
            if str(row["status"]) == "approved":
                raise RuntimeError("Approved revert intent cannot be reversed without recovery")
            updated = proposal.model_copy(update={"status": "rejected"})
            stored = self.state_controller.decide_harness_dream_revert(
                proposal_id, expected_revision=request.expected_revision,
                status="rejected", proposal=updated.model_dump(mode="json"),
            )
            self.outbox.wake()
            return stored
        if proposal.status not in {"proposed", "approved"} or not proposal.candidate_commit:
            raise RuntimeError("Blocked revert candidate must be repaired before approval")
        revision = request.expected_revision
        if proposal.status != "approved":
            intent = proposal.model_copy(update={"status": "approved"})
            intent_row = self.state_controller.decide_harness_dream_revert(
                proposal_id, expected_revision=revision, status="approved",
                proposal=intent.model_dump(mode="json"),
            )
            revision = int(intent_row["revision"])
            proposal = intent
        updated = await asyncio.to_thread(self._merge_harness_dream_revert, proposal)
        stored = self.state_controller.decide_harness_dream_revert(
            proposal_id, expected_revision=revision,
            status=updated.status, proposal=updated.model_dump(mode="json"),
        )
        self.outbox.wake()
        return {**stored, "proposal": updated.model_dump(mode="json")}

    def _merge_harness_dream_revert(
        self, proposal: HarnessRevertProposal,
    ) -> HarnessRevertProposal:
        source_root = (self.config.coding_source_root or Path(__file__).resolve().parents[1]).resolve()
        env = SensitiveEnvSanitizer.subprocess_env({"GIT_TERMINAL_PROMPT": "0"})
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=source_root, env=env,
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        ).stdout.decode("utf-8", errors="replace").strip()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source_root, env=env,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        ).stdout.decode().strip()
        if not status and head == proposal.candidate_commit:
            return proposal.model_copy(update={"status": "merged"})
        if status or head != proposal.base_head:
            return proposal.model_copy(update={"status": "blocked"})
        merged = subprocess.run(
            ["git", "merge", "--ff-only", proposal.candidate_commit],
            cwd=source_root, env=env, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        if merged.returncode != 0:
            return proposal.model_copy(update={"status": "blocked"})
        return proposal.model_copy(update={"status": "merged"})

    async def run_dream(self, selected: str | None = None):
        if not self.pool.is_idle():
            raise RuntimeError("普通 Agent 任务仍在运行，Dream 将在任务结束后执行")
        target = date.fromisoformat(selected) if selected else datetime.now().astimezone().date() - timedelta(days=1)
        result = await self._execute_dream_day(target, automatic=False)
        await self._record_dream_result(result, False)
        await self.checkpoint_dream.process_due(target)
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
            persistence_contract=PersistenceContract.CONTROL_ONLY,
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
        await self._finalize_control_plane(state.run_id)
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

    async def recover_run(self, run_id: str, request: RecoveryDecisionRequest):
        result = self.state_controller.apply(RecoveryDecisionCommand(
            command_id=request.command_id, run_id=run_id,
            expected_revision=request.expected_revision, gateway_epoch=self.gateway_epoch,
            action=request.action, operation_id=request.operation_id,
            actor=request.actor, reason=request.reason,
            observed_result=request.observed_result,
            risk_confirmed=request.risk_confirmed,
        ))
        self.outbox.wake()
        if (
            request.action == "retry"
            and request.operation_id is None
            and result.state.task_state is TaskState.RECOVERY_REQUIRED
            and result.state.finalize_generation is not None
        ):
            await self.pool.finalizer.recover_generation(run_id, request.reason)
            return self.state_controller.state(run_id)
        return result

    async def _submit_cron_dispatch(self, dispatch: CronDispatch) -> None:
        job = await self.cron_service.get(dispatch.job_id)
        run_id = hashlib.sha256(f"cron-run:{dispatch.dispatch_id}".encode()).hexdigest()[:32]
        session_id = hashlib.sha256(f"cron-session:{dispatch.dispatch_id}".encode()).hexdigest()[:16]
        self.store.project(job.project_id)
        state, duplicate = self.state_controller.create_run(
            run_id=run_id,
            workload_kind=WorkloadKind.CRON,
            project_id=job.project_id,
            client_id=f"cron:{job.job_id}",
            task=job.prompt,
            idempotency_key=f"cron:{dispatch.dispatch_id}",
            request_hash=dispatch.request_hash,
            persistence_contract=PersistenceContract.SESSION_BACKED_WORKLOAD,
            session_id=session_id,
        )
        operation_id = hashlib.sha256(f"cron-operation:{dispatch.dispatch_id}".encode()).hexdigest()[:32]
        attempt_id = hashlib.sha256(f"cron-attempt:{dispatch.dispatch_id}".encode()).hexdigest()[:32]
        need_binding_receipt = not duplicate
        if duplicate:
            try:
                self.state_controller.operation(operation_id)
            except KeyError:
                # Crash after create_run but before the binding receipt: finish
                # the same deterministic binding, never create another Run.
                need_binding_receipt = True
        if need_binding_receipt:
            state = self.state_controller.apply(CreateOperationWithAttemptCommand(
                command_id=f"cron-bind:{dispatch.dispatch_id}:create", run_id=run_id,
                expected_revision=state.revision, gateway_epoch=self.gateway_epoch,
                operation_id=operation_id, attempt_id=attempt_id, turn_id=state.turn_id,
                kind=OperationKind.CRON_DISPATCH, name="cron_dispatch",
                stable_key=f"cron-dispatch:{dispatch.dispatch_id}", request_hash=dispatch.request_hash,
                idempotency=ToolIdempotency.PURE, side_effecting=False,
                external_idempotency_key=f"cron:{dispatch.dispatch_id}",
                retry_policy_snapshot=RetryPolicySnapshot(max_attempts=1, base_seconds=0.0, max_seconds=0.0,
                    automatic=False, requires_reconcile=False, requires_human_confirmation=False),
            )).state
            state = self.state_controller.apply(StartOperationAttemptCommand(
                command_id=f"cron-bind:{dispatch.dispatch_id}:start", run_id=run_id,
                expected_revision=state.revision, gateway_epoch=self.gateway_epoch, attempt_id=attempt_id,
            )).state
            receipt = json.dumps({"dispatch_id": dispatch.dispatch_id, "job_id": job.job_id,
                "job_revision": dispatch.job_revision, "session_id": session_id, "run_id": run_id,
                "request_hash": dispatch.request_hash}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            state = self.state_controller.apply(CompleteOperationAttemptCommand(
                command_id=f"cron-bind:{dispatch.dispatch_id}:complete", run_id=run_id,
                expected_revision=state.revision, gateway_epoch=self.gateway_epoch, attempt_id=attempt_id,
                result=receipt, result_hash=hashlib.sha256(receipt.encode()).hexdigest(), result_source="cron_binding",
            )).state
        if state.task_state is TaskState.CREATED:
            state = self.state_controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.gateway_epoch,
            task_state=TaskState.QUEUED,
            reason="Cron Scheduler 提交任务",
            )).state
        await self.cron_store.bind_running(dispatch.dispatch_id, claim_token=str(dispatch.claim_token),
                                           session_id=session_id, run_id=run_id,
                                           operation_id=operation_id, attempt_id=attempt_id)
        self.outbox.wake()
        run = self.store.run(state.run_id)
        current_state = self.state_controller.state(run.run_id)
        if current_state.task_state is TaskState.QUEUED:
            await self.pool.submit(run)
        elif current_state.task_state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED}:
            await self._settle_cron_terminal(run)

    async def _settle_cron_terminal(self, run) -> None:
        """Project a terminal Run into its scheduling-domain Dispatch exactly once."""
        try:
            row = await self.cron_store.dispatch_by_run_id(run.run_id)
        except KeyError:
            return
        if row.status not in {"running", "recovery_required"}:
            return
        status = "succeeded" if run.status == "completed" else "failed"
        await self.cron_store.mark_terminal(
            row.dispatch_id,
            status=status,
            result=run.answer if status == "succeeded" else None,
            error=run.error if status == "failed" else None,
        )

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
    RetryPolicySnapshot,
    StartOperationAttemptCommand,
    ToolIdempotency,
