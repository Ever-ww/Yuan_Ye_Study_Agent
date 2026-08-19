"""Gateway 托管的 Runtime 缓存、并发队列和取消控制。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from Agent import AgentRuntime, EventType, ExtensionCatalog, ModelRetryPolicy, RuntimeFailure, load_runtime_config
from Agent.hook import HookRegistry
from Agent.state import (
    BindSessionCommand,
    ExecutionOutcome,
    ExecutionState,
    RecordRuntimeEventCommand,
    RequestCancellationCommand,
    TaskState,
    TerminalTarget,
    TransitionCommand,
    is_runnable,
)
from gateway.approval import GatewayApprovalBroker
from gateway.durable_execution import DurableModelHooks, DurableToolCoordinator
from gateway.events import GatewayEventBus
from gateway.finalize import FinalizeCoordinator
from gateway.models import ApprovalRequest, RunRecord
from gateway.outbox import OutboxDispatcher
from gateway.state_controller import StateController
from gateway.store import GatewayStore
from gateway.session_reservation import SessionReservationRegistry
from memory import MemoryStore
from reference import ReferenceService
from backup import AgentHomeWriteGate, QuiesceResult


RuntimeFactory = Callable[[Path, GatewayApprovalBroker], AgentRuntime]
CronTerminalCallback = Callable[[RunRecord], Awaitable[None]]


@dataclass
class RuntimeEntry:
    runtime: AgentRuntime
    last_used: float


class RuntimePool:
    """每个 Session 缓存一个 Runtime，不同 Session 按全局上限并发。"""

    def __init__(
        self,
        *,
        agent_root: Path,
        store: GatewayStore,
        events: GatewayEventBus,
        max_concurrent_runs: int = 4,
        idle_timeout_seconds: int = 900,
        approval_timeout_seconds: int = 30,
        runtime_factory: RuntimeFactory | None = None,
        extensions: ExtensionCatalog | None = None,
        cron_service=None,
        reference_service: ReferenceService | None = None,
        state_controller: StateController,
        outbox: OutboxDispatcher | None = None,
        tool_retry_max_attempts: int = 3,
        tool_retry_base_seconds: float = 2.0,
        tool_retry_max_seconds: float = 60.0,
        write_gate: AgentHomeWriteGate | None = None,
        session_reservations: SessionReservationRegistry | None = None,
        finalizer: FinalizeCoordinator | None = None,
        harness_evolution_service=None,
        cron_tool_authorizer=None,
        cron_terminal_callback: CronTerminalCallback | None = None,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.store = store
        self.events = events
        self.max_concurrent_runs = max_concurrent_runs
        self.idle_timeout_seconds = idle_timeout_seconds
        self.runtime_factory = runtime_factory
        self.extensions = extensions
        self.cron_service = cron_service
        self.reference_service = reference_service
        self.state_controller = state_controller
        self.outbox = outbox
        self.write_gate = write_gate
        self.tool_operations = DurableToolCoordinator(
            state_controller,
            retry_max_attempts=tool_retry_max_attempts,
            retry_base_seconds=tool_retry_base_seconds,
            retry_max_seconds=tool_retry_max_seconds,
        )
        self.approvals = GatewayApprovalBroker(
            store,
            self._publish_approval,
            self.events.wait_connected,
            state_controller=state_controller,
            approval_timeout_seconds=approval_timeout_seconds,
            cron_tool_authorizer=cron_tool_authorizer,
        )
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._runtimes: dict[tuple[str, str], RuntimeEntry] = {}
        self.session_reservations = session_reservations or SessionReservationRegistry()
        self.finalizer = finalizer or FinalizeCoordinator(
            controller=state_controller,
            store=store,
            agent_root=self.agent_root,
            reservations=self.session_reservations,
        )
        self.harness_evolution_service = harness_evolution_service
        self.cron_terminal_callback = cron_terminal_callback
        self._pending_profile_refresh: set[tuple[str, str]] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._submitting: set[str] = set()
        self._closing = False
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        self._maintenance_epoch: int | None = None

    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle(), name="gateway-runtime-reaper")

    async def submit(self, run: RunRecord) -> None:
        if self._closing or self._maintenance_epoch is not None:
            raise RuntimeError("Gateway 正在关闭，不能接收新任务")
        async with self._lock:
            existing = self._tasks.get(run.run_id)
            if existing is not None and not existing.done():
                return
            if run.run_id in self._submitting:
                return
            self._submitting.add(run.run_id)
        try:
            state = self.state_controller.state(run.run_id)
            if state.task_state in {
                TaskState.SUCCEEDED, TaskState.FAILED,
                TaskState.CANCELLED, TaskState.INTERRUPTED,
            }:
                return
            if state.task_state is TaskState.RECOVERY_REQUIRED:
                raise RuntimeError("RECOVERY_REQUIRED Run must be handled by RecoveryCoordinator")
            operation = self.state_controller.active_operation(run.run_id)
            attempt = (
                self.state_controller.current_attempt(operation.operation_id)
                if operation is not None else None
            )
            if state.gateway_epoch != self.state_controller.gateway_epoch:
                raise RuntimeError("Run fencing token 不属于当前 Gateway epoch")
            if not is_runnable(state, operation, attempt, now=datetime.now().astimezone()):
                raise RuntimeError(f"Run 当前不可调度：{state.task_state.value}")
            if run.session_id:
                await self.session_reservations.acquire(
                    run.project_id, run.session_id, owner_id=run.run_id, wait=False,
                )
            started = asyncio.Event()
            task = asyncio.create_task(
                self._execute_with_write_scope(run, started),
                name=f"gateway-run-{run.run_id}",
            )
            async with self._lock:
                self._tasks[run.run_id] = task
            await started.wait()
            if task.done() and task.exception() is not None:
                raise task.exception()
        finally:
            async with self._lock:
                self._submitting.discard(run.run_id)

    async def _execute_with_write_scope(self, run: RunRecord, started: asyncio.Event) -> None:
        if self.write_gate is None:
            started.set()
            await self._execute(run)
            return
        try:
            async with self.write_gate.operation("runtime_pool", run.run_id):
                started.set()
                await self._execute(run)
        finally:
            started.set()

    async def cancel(self, run_id: str) -> bool:
        state = self.state_controller.state(run_id)
        if state.task_state not in {
            TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED,
        } and not state.cancellation_requested:
            self.state_controller.apply(RequestCancellationCommand(
                command_id=uuid4().hex,
                run_id=run_id,
                expected_revision=state.revision,
                gateway_epoch=self.state_controller.gateway_epoch,
                reason="客户端请求取消",
            ))
            if self.outbox:
                self.outbox.wake()
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self, grace_seconds: float = 5.0) -> None:
        self._closing = True
        await self.approvals.deny_all()
        active = [task for task in self._tasks.values() if not task.done()]
        if active:
            done, pending = await asyncio.wait(active, timeout=grace_seconds)
            del done
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        entries = list(self._runtimes.values())
        self._runtimes.clear()
        for entry in entries:
            await entry.runtime.close()

    async def refresh_skills(self, project_id: str, session_id: str):
        """刷新指定的持久化 Session；空闲时自动恢复 Runtime。"""
        if self._maintenance_epoch is not None:
            raise RuntimeError("Agent Home 正在维护，不能刷新 Skill")
        key = (project_id, session_id)
        reservation_owner = f"skill-refresh:{project_id}:{session_id}:{uuid4().hex}"
        await self.session_reservations.acquire(
            project_id, session_id, owner_id=reservation_owner, wait=False,
        )
        created = False
        try:
            entry = self._runtimes.get(key)
            if entry is None:
                project = self.store.project(project_id)
                workspace = Path(project.path)
                memory = MemoryStore(
                    self.agent_root / ".yy" / "memory",
                    workspace_root=workspace,
                    agent_root=self.agent_root,
                )
                if not memory.has_session(session_id):
                    raise RuntimeError(f"项目中不存在 Session：{session_id}")
                placeholder = RunRecord(
                    run_id=f"skill-refresh-{uuid4().hex}",
                    project_id=project_id,
                    session_id=session_id,
                    client_id="gateway:skill-refresh",
                    task="/skill refresh",
                    status="completed",
                    created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                )
                runtime = await self._runtime_for(placeholder, workspace)
                entry = RuntimeEntry(runtime, monotonic())
                self._runtimes[key] = entry
                created = True
            entry.last_used = monotonic()
            return await entry.runtime.refresh_skills(session_id)
        except Exception:
            if created:
                selected = self._runtimes.pop(key, None)
                if selected is not None:
                    await selected.runtime.close()
            raise
        finally:
            await self.session_reservations.release_owner(reservation_owner)

    def is_idle(self) -> bool:
        """Dream 只在没有排队或运行任务时读取 Session 与修改 Profile。"""
        return not self.session_reservations.busy_keys and not any(
            not task.done() for task in self._tasks.values()
        )

    def has_active_run(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult:
        if self._maintenance_epoch is not None and maintenance_epoch <= self._maintenance_epoch:
            return QuiesceResult(
                participant="runtime_pool",
                maintenance_epoch=maintenance_epoch,
                acknowledged=maintenance_epoch == self._maintenance_epoch,
                stale=maintenance_epoch < self._maintenance_epoch,
                safe_boundary="all_active_runs_durable" if maintenance_epoch == self._maintenance_epoch else None,
            )
        self._maintenance_epoch = maintenance_epoch
        active = [task for task in self._tasks.values() if not task.done()]
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        return QuiesceResult(
            participant="runtime_pool",
            maintenance_epoch=maintenance_epoch,
            acknowledged=True,
            safe_boundary="all_active_runs_durable",
        )

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None

    async def invalidate_profile_context(self, after_active_turn: bool = True) -> None:
        """Profile 更新后刷新空闲 Runtime；活动 Turn 在结束后再刷新。"""
        async with self._lock:
            for key, entry in self._runtimes.items():
                if after_active_turn and key in self.session_reservations.busy_keys:
                    self._pending_profile_refresh.add(key)
                else:
                    entry.runtime.invalidate_context_cache()

    async def _execute(self, original: RunRecord) -> None:
        runtime: AgentRuntime | None = None
        operation_token = None
        answer = ""
        stateless_cron = original.client_id.startswith("cron:")
        session_key: tuple[str, str] | None = (
            (original.project_id, original.session_id) if original.session_id else None
        )
        try:
            await self._emit(original, "run_queued", {"message": "任务已进入 Gateway 队列"})
            async with self._semaphore:
                selected = self.state_controller.state(original.run_id)
                if selected.task_state is TaskState.QUEUED:
                    selected = await self._transition(
                        original.run_id,
                        task_state=TaskState.STARTING,
                        reason="Scheduler durable claim",
                    )
                elif selected.task_state is not TaskState.STARTING:
                    raise RuntimeError(f"Run lost durable submit claim: {selected.task_state.value}")
                await self._transition(
                    original.run_id,
                    task_state=TaskState.RUNNING,
                    execution_state=ExecutionState.THINKING if selected.execution is None else None,
                    reason="Runtime 初始化完成",
                )
                current = self.store.run(original.run_id)
                await self._emit(current, "run_started", {"message": "任务开始运行"})
                project = self.store.project(current.project_id)
                runtime = await self._runtime_for(current, Path(project.path))
                durable_state = self.state_controller.state(current.run_id)
                operation_token = self.tool_operations.bind(
                    current.run_id,
                    durable_state.turn_id,
                )
                token = self.approvals.bind_run(current.run_id, current.client_id)
                try:
                    async for event in runtime.run_task(current.task, current.session_id):
                        if event.type is EventType.STARTED:
                            session_id = str(event.payload["session_id"])
                            if stateless_cron:
                                await self._emit(
                                    current, "cron_ephemeral_context_started",
                                    {"session_id": session_id, "persisted": True, "origin": "cron"},
                                )
                                continue
                            state = self.state_controller.state(current.run_id)
                            self.state_controller.apply(BindSessionCommand(
                                command_id=uuid4().hex,
                                run_id=current.run_id,
                                expected_revision=state.revision,
                                gateway_epoch=self.state_controller.gateway_epoch,
                                session_id=session_id,
                            ))
                            current = self.store.run(current.run_id)
                            new_key = (current.project_id, session_id)
                            if session_key is None:
                                await self.session_reservations.bind(
                                    current.project_id, session_id,
                                    owner_id=current.run_id, wait=False,
                                )
                                session_key = new_key
                            self._runtimes[new_key] = RuntimeEntry(runtime, monotonic())
                        if event.type is EventType.FINAL:
                            answer = str(event.payload.get("answer", ""))
                        await self._emit(
                            current,
                            event.type.value,
                            dict(event.payload),
                        )
                    await self._finish_run(current.run_id, ExecutionOutcome.SUCCESS, answer or "任务完成")
                    current = self.store.run(current.run_id)
                    await self._emit(current, "run_completed", {"answer": answer})
                finally:
                    self.approvals.reset_run(token)
        except asyncio.CancelledError:
            state = self.state_controller.state(original.run_id)
            if state.task_state is not TaskState.RECOVERY_REQUIRED:
                await self._finish_run(original.run_id, ExecutionOutcome.CANCELLED, "当前运行已取消")
            current = self.store.run(original.run_id)
            await self._emit(current, "run_cancelled", {"message": "当前运行已取消"})
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            failure = (
                runtime.last_failure
                if runtime is not None and getattr(runtime, "last_failure", None) is not None
                else RuntimeFailure.capture(exc)
            )
            proposal = None
            if self.harness_evolution_service is not None:
                try:
                    proposal = self.harness_evolution_service.propose_error(
                        run_id=original.run_id, failure=failure,
                    )
                except Exception as proposal_error:
                    await self._emit(original, "harness_evolution_proposal_failed", {
                        "message": str(proposal_error) or type(proposal_error).__name__,
                    })
            if proposal is not None:
                # The client subscription terminates at run_failed. Proposal must be emitted
                # first so the originating user can make the one durable decision.
                await self._emit(original, "harness_evolution_proposed", proposal)
            state = self.state_controller.state(original.run_id)
            if state.task_state is not TaskState.RECOVERY_REQUIRED:
                await self._finish_run(original.run_id, ExecutionOutcome.ERROR, message)
            current = self.store.run(original.run_id)
            await self._emit(current, "run_failed", {"message": current.error or "运行失败"})
        finally:
            if operation_token is not None:
                self.tool_operations.reset(operation_token)
            current = self.store.run(original.run_id)
            if current.status in {"completed", "failed", "cancelled", "interrupted"}:
                item = next(
                    (entry for entry in self.store.list_inbox() if entry.run_id == current.run_id),
                    None,
                )
                if item is not None:
                    state = self.state_controller.state(current.run_id)
                    self.state_controller.apply(RecordRuntimeEventCommand(
                        command_id=f"finalize:{current.run_id}:inbox-announcement",
                        run_id=current.run_id,
                        expected_revision=state.revision,
                        gateway_epoch=self.state_controller.gateway_epoch,
                        event_type="inbox_created",
                        payload=item.model_dump(mode="json"),
                    ))
                    if self.outbox:
                        self.outbox.wake()
                        await self.outbox.drain_once()
            if runtime is not None and session_key is not None:
                entry = self._runtimes.get(session_key)
                if entry is not None:
                    entry.last_used = monotonic()
            if runtime is not None and stateless_cron:
                await runtime.close()
            if session_key is not None:
                await self.session_reservations.release_owner(original.run_id)
                async with self._lock:
                    if session_key in self._pending_profile_refresh:
                        entry = self._runtimes.get(session_key)
                        if entry is not None:
                            entry.runtime.invalidate_context_cache()
                        self._pending_profile_refresh.discard(session_key)
            self._tasks.pop(original.run_id, None)

    async def _transition(
        self,
        run_id: str,
        *,
        task_state: TaskState | None = None,
        execution_state: ExecutionState | None = None,
        outcome: ExecutionOutcome | None = None,
        finish_reason: str | None = None,
        terminal_target: TerminalTarget | None = None,
        reason: str,
        error: str | None = None,
        result_summary: str | None = None,
    ):
        state = self.state_controller.state(run_id)
        result = self.state_controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch=self.state_controller.gateway_epoch,
            task_state=task_state,
            execution_state=execution_state,
            outcome=outcome,
            finish_reason=finish_reason,
            terminal_target=terminal_target,
            reason=reason,
            error=error,
            result_summary=result_summary,
        ))
        if self.outbox:
            self.outbox.wake()
        return result.state

    async def _finish_run(self, run_id: str, outcome: ExecutionOutcome, reason: str) -> None:
        state = self.state_controller.state(run_id)
        if state.task_state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED}:
            return
        if state.execution is not None and state.execution.state is not ExecutionState.FINISHED:
            state = await self._transition(
                run_id,
                execution_state=ExecutionState.FINISHED,
                outcome=outcome,
                finish_reason=reason,
                reason="Agent 内层执行结束",
            )
        if outcome is ExecutionOutcome.CANCELLED and state.task_state is not TaskState.CANCELLING:
            state = await self._transition(run_id, task_state=TaskState.CANCELLING, reason="执行取消收尾")
        target = {
            ExecutionOutcome.SUCCESS: TerminalTarget.SUCCEEDED,
            ExecutionOutcome.ERROR: TerminalTarget.FAILED,
            ExecutionOutcome.EXHAUSTED: TerminalTarget.FAILED,
            ExecutionOutcome.CANCELLED: TerminalTarget.CANCELLED,
        }[outcome]
        state = await self._transition(
            run_id,
            task_state=TaskState.FINALIZING,
            terminal_target=target,
            reason="进入幂等 FINALIZING",
            error=reason if outcome in {ExecutionOutcome.ERROR, ExecutionOutcome.EXHAUSTED} else None,
            result_summary=reason if outcome is ExecutionOutcome.SUCCESS else None,
        )
        await self.finalizer.finalize(run_id)
        if self.cron_terminal_callback is not None:
            final_run = self.store.run(run_id)
            if final_run.client_id.startswith("cron:"):
                await self.cron_terminal_callback(final_run)
        if self.outbox:
            self.outbox.wake()

    async def _runtime_for(self, run: RunRecord, workspace: Path) -> AgentRuntime:
        if run.session_id:
            entry = self._runtimes.get((run.project_id, run.session_id))
            if entry is not None:
                entry.last_used = monotonic()
                return entry.runtime
        if self.runtime_factory is not None:
            runtime = self.runtime_factory(workspace, self.approvals)
        else:
            runtime = self._default_runtime(workspace, self.approvals, run)
        if run.client_id.startswith("cron:") and self.cron_service is not None:
            # The dispatch snapshot, not today's editable CronJob, is the
            # authority for an already materialized unattended run.
            dispatch = await self.cron_service.store.dispatch_by_run_id(run.run_id)
            snapshot = dispatch.job_snapshot
            profile = snapshot.get("runtime_profile", {}) if isinstance(snapshot, dict) else {}
            allowed_tools = tuple(profile.get("allowed_tools", ())) if isinstance(profile, dict) else ()
            # Cron starts from a deny-all surface. A durable snapshot selects tools.
            runtime.tools = runtime.tools.select(allowed_tools)
        if (
            self.harness_evolution_service is not None
            and not run.client_id.startswith("cron:")
            and hasattr(runtime, "tools")
            and "harness_capability" not in runtime.tools.names()
        ):
            from tools import HarnessCapabilityTool
            runtime.tools.register(HarnessCapabilityTool(self.harness_evolution_service))
        if (
            self.tool_operations is not None
            and hasattr(runtime, "tool_context")
            and hasattr(runtime, "hooks")
        ):
            runtime.tool_context = runtime.tool_context.model_copy(
                update={"operation_coordinator": self.tool_operations},
            )
            runtime_config = getattr(runtime, "config", None)
            retry_policy = None
            if runtime_config is not None:
                from Agent.state import RetryPolicySnapshot
                retry_policy = RetryPolicySnapshot(
                    max_attempts=runtime_config.model_retry_max_attempts,
                    base_seconds=runtime_config.model_retry_base_seconds,
                    max_seconds=runtime_config.model_retry_max_seconds,
                    automatic=True, requires_reconcile=False,
                    requires_human_confirmation=False,
                )
            DurableModelHooks(
                self.state_controller,
                None if run.client_id.startswith("cron:") else getattr(runtime, "memory", None),
                retry_policy=retry_policy,
            ).register(runtime.hooks)
        if hasattr(runtime, "bind_gateway_run"):
            runtime.bind_gateway_run(run.run_id)
        return runtime

    def _default_runtime(
        self,
        workspace: Path,
        approvals: GatewayApprovalBroker,
        run: RunRecord,
    ) -> AgentRuntime:
        config = load_runtime_config(self.agent_root, workspace_root=workspace)
        scheduled = run.client_id.startswith("cron:")
        if scheduled:
            from skill import SkillService
            config = config.model_copy(update={"stream": False, "compression_threshold_tokens": 0})
            skills = SkillService(config.agent_root, workspace, config.coding_source_root)
            # A Cron dispatch has a fresh durable Session but never restores another Session.
            memory = MemoryStore(
                config.memory_dir,
                workspace_root=config.workspace_root,
                agent_root=config.agent_root,
            )
            hooks = HookRegistry()
            return AgentRuntime(
                config,
                memory=memory,
                hooks=hooks,
                skills=skills,
                approval=approvals,
                enable_context_processing=False,
                enable_subagent=False,
                enable_extensions=False,
                retry_policy=ModelRetryPolicy(
                    max_attempts=config.model_retry_max_attempts,
                    delay_seconds=config.model_retry_base_seconds,
                ),
                raise_errors=True,
                extensions=self.extensions,
                enable_cron=False,
                session_origin="cron",
                references=self.reference_service,
                enable_references=self.reference_service is not None,
                runtime_profile="cron",
            )
        runtime = AgentRuntime(
            config,
            approval=approvals,
            retry_policy=ModelRetryPolicy(
                max_attempts=config.model_retry_max_attempts,
                delay_seconds=config.model_retry_base_seconds,
            ),
            raise_errors=True,
            extensions=self.extensions,
            cron=self.cron_service if not scheduled else None,
            cron_project_id=run.project_id,
            enable_cron=not scheduled,
            session_origin="cron" if scheduled else "interactive",
            references=self.reference_service,
            enable_references=self.reference_service is not None,
            runtime_profile="interactive",
            extension_state=self.state_controller,
        )
        return runtime

    async def _emit(self, run: RunRecord, event_type: str, payload: dict) -> None:
        state = self.state_controller.state(run.run_id)
        self.state_controller.apply(RecordRuntimeEventCommand(
            command_id=uuid4().hex,
            run_id=run.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.state_controller.gateway_epoch,
            event_type=event_type,
            payload=payload,
            mark_progress=event_type in {"text", "model_reconnected", "tool_completed"},
        ))
        if self.outbox is not None:
            self.outbox.wake()
            await self.outbox.drain_once()

    async def _publish_approval(self, request: ApprovalRequest) -> None:
        run = self.store.run(request.run_id)
        await self._emit(run, "approval_requested", request.model_dump(mode="json"))

    async def _reap_idle(self) -> None:
        interval = max(5.0, min(60.0, self.idle_timeout_seconds / 2))
        while True:
            await asyncio.sleep(interval)
            now = monotonic()
            selected: list[RuntimeEntry] = []
            async with self._lock:
                for key, entry in tuple(self._runtimes.items()):
                    if key not in self.session_reservations.busy_keys and now - entry.last_used >= self.idle_timeout_seconds:
                        selected.append(entry)
                        self._runtimes.pop(key, None)
            for entry in selected:
                await entry.runtime.close()
