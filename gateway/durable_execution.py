"""把 Tool Registry 接到 Durable Operation Ledger 的两阶段边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from Agent.state import (
    AbandonOperationAttemptCommand,
    BeginOperationAttemptCommand,
    CompleteOperationAttemptCommand,
    CreateOperationWithAttemptCommand,
    CreateSafeCheckpointCommand,
    ExecutionOutcome,
    ExecutionState,
    FailOperationAttemptCommand,
    MarkOperationAttemptUnknownCommand,
    OperationKind,
    OperationFailureKind,
    OperationRecord,
    OperationStatus,
    RetryPolicySnapshot,
    SkipOperationAttemptCommand,
    StartOperationAttemptCommand,
    HeartbeatOperationAttemptCommand,
    SafeCheckpoint,
    TaskState,
    ToolIdempotency,
    TransitionCommand,
)
from Agent.hook import HookEvent, HookPoint, HookRegistry
from gateway.models import now_iso
from gateway.state_controller import StateController
from tool.errors import ToolExecutionObservationError


@dataclass(frozen=True)
class DurableRunBinding:
    run_id: str
    turn_id: str


_RUN_BINDING: ContextVar[DurableRunBinding | None] = ContextVar("durable_run_binding", default=None)
_CURRENT_OPERATION: ContextVar[str | None] = ContextVar("durable_operation_id", default=None)
_CURRENT_ATTEMPT: ContextVar[str | None] = ContextVar("durable_attempt_id", default=None)


def current_operation_id() -> str | None:
    return _CURRENT_OPERATION.get()


def current_attempt_id() -> str | None:
    return _CURRENT_ATTEMPT.get()


class DurableToolCoordinator:
    """真实 Tool 副作用前后分别提交 Ledger，崩溃窗口保持 unknown。"""

    def __init__(
        self,
        controller: StateController,
        *,
        heartbeat_seconds: int = 60,
        retry_max_attempts: int = 3,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 60.0,
    ) -> None:
        self.controller = controller
        self.heartbeat_seconds = max(5, heartbeat_seconds)
        self.retry_max_attempts = retry_max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    def bind(self, run_id: str, turn_id: str | None = None) -> Token:
        return _RUN_BINDING.set(DurableRunBinding(run_id, turn_id or uuid4().hex))

    def reset(self, token: Token) -> None:
        _RUN_BINDING.reset(token)

    async def prepare(
        self,
        *,
        tool: Any,
        name: str,
        arguments: dict[str, Any],
        risk: str,
        context: Any,
        tool_call_id: str,
    ) -> OperationRecord:
        binding = self._binding()
        parent_operation_id = _CURRENT_OPERATION.get()
        selected = _idempotency_of(tool, risk)
        frozen_request = {
            "name": name,
            "arguments": arguments,
            "workspace_root": str(context.project_root.resolve()),
            "risk": risk,
        }
        serialized = json.dumps(frozen_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        state = self.controller.state(binding.run_id)
        if state.execution and state.execution.state is ExecutionState.OBSERVING:
            state = self.controller.apply(TransitionCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                execution_state=ExecutionState.THINKING,
                reason="处理同一模型回复中的下一个工具调用",
            )).state
        source_model_call_id = state.model_call_id or "model-call-unavailable"
        stable_key = f"tool:{binding.turn_id}:{source_model_call_id}:{tool_call_id}"
        automatic = selected is not ToolIdempotency.NON_IDEMPOTENT
        result = self.controller.apply(CreateOperationWithAttemptCommand(
            command_id=uuid4().hex,
            run_id=binding.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=uuid4().hex,
            attempt_id=uuid4().hex,
            parent_operation_id=parent_operation_id,
            turn_id=binding.turn_id,
            kind=OperationKind.TOOL,
            name=name,
            stable_key=stable_key,
            request_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            idempotency=selected,
            side_effecting=risk != "read",
            source_model_call_id=source_model_call_id,
            tool_call_id=tool_call_id,
            external_request_id=uuid4().hex if risk != "read" else None,
            external_idempotency_key=hashlib.sha256(stable_key.encode("utf-8")).hexdigest(),
            retry_policy_snapshot=RetryPolicySnapshot(
                max_attempts=self.retry_max_attempts,
                base_seconds=self.retry_base_seconds,
                max_seconds=self.retry_max_seconds,
                automatic=automatic,
                requires_reconcile=selected is ToolIdempotency.EXTERNALLY_IDEMPOTENT,
                requires_human_confirmation=selected is ToolIdempotency.NON_IDEMPOTENT,
            ),
        ))
        assert result.operation is not None and result.attempt is not None
        _CURRENT_OPERATION.set(result.operation.operation_id)
        _CURRENT_ATTEMPT.set(result.attempt.attempt_id)
        return result.operation

    async def waiting_human(self, operation: OperationRecord) -> None:
        state = self.controller.state(operation.run_id)
        if state.execution is None or state.execution.state is not ExecutionState.THINKING:
            raise RuntimeError("工具审批只能从 THINKING 进入 WAITING_HUMAN")
        self.controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            execution_state=ExecutionState.WAITING_HUMAN,
            reason=f"工具 {operation.name} 等待用户审批",
        ))

    async def approval_decided(self, operation: OperationRecord, *, approved: bool) -> str | None:
        state = self.controller.state(operation.run_id)
        if approved:
            self.controller.apply(TransitionCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                execution_state=ExecutionState.ACTING,
                reason=f"工具 {operation.name} 审批通过",
            ))
            return None
        attempt_id = _CURRENT_ATTEMPT.get()
        if attempt_id is None:
            raise RuntimeError("Approval 拒绝时缺少对应 Attempt")
        current_attempt = self.controller.current_attempt(operation.operation_id)
        if current_attempt.status is OperationStatus.SKIPPED:
            skipped_state = state
        else:
            skipped_state = self.controller.apply(SkipOperationAttemptCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt_id,
                skip_reason="approval_denied_or_timeout",
            )).state
        self.controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=skipped_state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            execution_state=ExecutionState.OBSERVING,
            reason=f"工具 {operation.name} 未执行，记录结构化 observation",
        ))
        _CURRENT_OPERATION.set(None)
        _CURRENT_ATTEMPT.set(None)
        return json.dumps({
            "status": "NOT_EXECUTED", "tool": operation.name,
            "reason": "approval_denied_or_timeout",
        }, ensure_ascii=False)

    async def execute(
        self,
        operation: OperationRecord,
        invoke: Callable[[], Awaitable[str]],
    ) -> str:
        state = self.controller.state(operation.run_id)
        if state.execution is None:
            raise RuntimeError("Tool Operation 缺少内层执行状态")
        if state.execution.state is ExecutionState.THINKING:
            state = self.controller.apply(TransitionCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                execution_state=ExecutionState.ACTING,
                reason=f"开始执行工具 {operation.name}",
            )).state
        elif state.execution.state is not ExecutionState.ACTING:
            raise RuntimeError(f"不能在 {state.execution.state.value} 状态执行工具")

        while True:
            attempt_id = _CURRENT_ATTEMPT.get()
            if attempt_id is None:
                raise RuntimeError("Tool Operation 缺少当前 Attempt")
            state = self.controller.state(operation.run_id)
            started = self.controller.apply(StartOperationAttemptCommand(
                command_id=uuid4().hex, run_id=state.run_id,
                expected_revision=state.revision, gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt_id, heartbeat_expires_at=_after_seconds(self.heartbeat_seconds),
            ))
            operation = started.operation or operation
            heartbeat = asyncio.create_task(
                self._heartbeat_loop(operation),
                name=f"tool-heartbeat-{operation.operation_id}:{attempt_id}",
            )
            try:
                result = await invoke()
            except asyncio.CancelledError as exc:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._record_uncertain_or_failed(operation, exc, cancelled=True)
                raise
            except BaseException as exc:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                if operation.idempotency in {
                    ToolIdempotency.EXTERNALLY_IDEMPOTENT,
                    ToolIdempotency.NON_IDEMPOTENT,
                }:
                    await self._record_uncertain_or_failed(operation, exc, cancelled=False)
                    raise
                retryable = _is_retryable_tool_error(exc)
                current = self.controller.state(operation.run_id)
                failed = self.controller.apply(FailOperationAttemptCommand(
                    command_id=uuid4().hex, run_id=current.run_id,
                    expected_revision=current.revision, gateway_epoch=self.controller.gateway_epoch,
                    attempt_id=attempt_id,
                    failure_kind=(
                        OperationFailureKind.RETRYABLE
                        if retryable else OperationFailureKind.TERMINAL
                    ),
                    failure_reason=str(exc) or type(exc).__name__,
                ))
                operation = failed.operation or operation
                policy = operation.retry_policy_snapshot
                if (
                    operation.failure_kind is not OperationFailureKind.RETRYABLE
                    or not policy.automatic
                    or operation.next_retry_at is None
                ):
                    current = self.controller.state(operation.run_id)
                    # PURE/IDEMPOTENT 的失败已经由 Ledger 确认为 FAILED，不存在未知
                    # 副作用窗口；将其作为 observation 交给模型重新规划。外部幂等和
                    # 非幂等工具在上方走 UNKNOWN + RECOVERY_REQUIRED，绝不进入这里。
                    self.controller.apply(TransitionCommand(
                        command_id=uuid4().hex, run_id=current.run_id,
                        expected_revision=current.revision, gateway_epoch=self.controller.gateway_epoch,
                        execution_state=ExecutionState.OBSERVING,
                        reason="Determined tool failure; return observation to model",
                    ))
                    _CURRENT_OPERATION.set(None)
                    _CURRENT_ATTEMPT.set(None)
                    raise ToolExecutionObservationError(
                        str(exc) or type(exc).__name__,
                    ) from exc
                retry_at = datetime.fromisoformat(operation.next_retry_at.replace("Z", "+00:00"))
                await asyncio.sleep(max(0.0, (retry_at - datetime.now().astimezone()).total_seconds()))
                current = self.controller.state(operation.run_id)
                next_attempt = self.controller.apply(BeginOperationAttemptCommand(
                    command_id=uuid4().hex, run_id=current.run_id,
                    expected_revision=current.revision, gateway_epoch=self.controller.gateway_epoch,
                    operation_id=operation.operation_id, attempt_id=uuid4().hex,
                    expected_latest_attempt_no=operation.latest_attempt_no,
                    request_hash=operation.request_hash,
                    external_request_id=uuid4().hex if operation.side_effecting else None,
                ))
                operation = next_attempt.operation or operation
                assert next_attempt.attempt is not None
                _CURRENT_ATTEMPT.set(next_attempt.attempt.attempt_id)
                continue
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            break
        selected = str(result)
        state = self.controller.state(operation.run_id)
        self.controller.apply(CompleteOperationAttemptCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            attempt_id=attempt_id,
            result=selected,
            result_hash=hashlib.sha256(selected.encode("utf-8")).hexdigest(),
            result_source="tool_return",
        ))
        state = self.controller.state(operation.run_id)
        self.controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            execution_state=ExecutionState.OBSERVING,
            reason=f"工具 {operation.name} 结果已提交 Ledger",
        ))
        _CURRENT_OPERATION.set(None)
        _CURRENT_ATTEMPT.set(None)
        return selected

    async def _heartbeat_loop(self, operation: OperationRecord) -> None:
        interval = max(2.0, self.heartbeat_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            current = self.controller.operation(operation.operation_id)
            if current.status is not OperationStatus.RUNNING:
                return
            state = self.controller.state(operation.run_id)
            timestamp = now_iso()
            attempt_id = _CURRENT_ATTEMPT.get()
            if attempt_id is None:
                return
            self.controller.apply(HeartbeatOperationAttemptCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt_id,
                heartbeat_at=timestamp,
                heartbeat_expires_at=_after_seconds(self.heartbeat_seconds),
            ))

    async def preexecution_failed_if_safe(self, operation: OperationRecord | None, error: BaseException) -> None:
        if operation is None:
            return
        current = self.controller.operation(operation.operation_id)
        if current.status is not OperationStatus.PREPARED:
            return
        attempt_id = _CURRENT_ATTEMPT.get()
        if attempt_id is None:
            return
        state = self.controller.state(operation.run_id)
        self.controller.apply(FailOperationAttemptCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            attempt_id=attempt_id,
            failure_kind=OperationFailureKind.TERMINAL,
            failure_reason=str(error) or type(error).__name__,
        ))
        state = self.controller.state(operation.run_id)
        if state.execution and state.execution.state is ExecutionState.WAITING_HUMAN:
            self.controller.apply(TransitionCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                execution_state=ExecutionState.FINISHED,
                outcome=ExecutionOutcome.ERROR,
                finish_reason=str(error) or type(error).__name__,
                reason="工具在真实副作用前失败",
            ))
        _CURRENT_OPERATION.set(None)
        _CURRENT_ATTEMPT.set(None)

    async def _record_uncertain_or_failed(
        self,
        operation: OperationRecord,
        error: BaseException,
        *,
        cancelled: bool,
    ) -> None:
        state = self.controller.state(operation.run_id)
        message = str(error) or type(error).__name__
        attempt_id = _CURRENT_ATTEMPT.get()
        if attempt_id is None:
            raise RuntimeError("Tool failure 缺少当前 Attempt")
        uncertain = operation.idempotency in {
            ToolIdempotency.EXTERNALLY_IDEMPOTENT,
            ToolIdempotency.NON_IDEMPOTENT,
        }
        if uncertain:
            self.controller.apply(MarkOperationAttemptUnknownCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt_id,
                failure_reason=("cancelled during external operation: " if cancelled else "tool raised after dispatch: ") + message,
            ))
            state = self.controller.state(operation.run_id)
            self.controller.apply(TransitionCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                task_state=TaskState.RECOVERY_REQUIRED,
                reason="外部工具副作用结果未知",
            ))
            return
        self.controller.apply(FailOperationAttemptCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            attempt_id=attempt_id,
            failure_kind=OperationFailureKind.RETRYABLE,
            failure_reason=message,
        ))
        state = self.controller.state(operation.run_id)
        self.controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            execution_state=ExecutionState.FINISHED,
            outcome=ExecutionOutcome.CANCELLED if cancelled else ExecutionOutcome.ERROR,
            finish_reason=message,
            reason="无未知副作用的工具执行终止",
        ))

    @staticmethod
    def _binding() -> DurableRunBinding:
        binding = _RUN_BINDING.get()
        if binding is None:
            raise RuntimeError("Durable Tool 未绑定 Gateway Run")
        return binding


class DurableModelHooks:
    """把每个真实模型 API attempt 记录为独立 Operation。"""

    def __init__(
        self,
        controller: StateController,
        memory: Any | None = None,
        *,
        retry_policy: RetryPolicySnapshot | None = None,
    ) -> None:
        self.controller = controller
        self.memory = memory
        self.retry_policy = retry_policy or RetryPolicySnapshot(
            max_attempts=3, base_seconds=2.0, max_seconds=30.0,
            automatic=True, requires_reconcile=False, requires_human_confirmation=False,
        )
        self._operation: ContextVar[str | None] = ContextVar("durable_model_operation", default=None)
        self._heartbeat: ContextVar[asyncio.Task[None] | None] = ContextVar("durable_model_heartbeat", default=None)

    def register(self, hooks: HookRegistry) -> None:
        hooks.register(HookPoint.TURN_START, self.annotate, priority=-200)
        hooks.register(HookPoint.MODEL_BEFORE, self.annotate, priority=-200)
        hooks.register(HookPoint.MODEL_DURING, self.model_during, priority=-100)
        hooks.register(HookPoint.MODEL_AFTER, self.model_after, priority=-90)
        hooks.register(HookPoint.TOOL_AFTER, self.annotate, priority=-90)
        hooks.register(HookPoint.TURN_END, self.annotate, priority=-90)
        if self.memory is not None:
            hooks.register(HookPoint.TOOL_AFTER, self.safe_checkpoint, priority=200)
            hooks.register(HookPoint.TURN_END, self.safe_checkpoint, priority=200)

    async def annotate(self, event: HookEvent) -> None:
        binding = _RUN_BINDING.get()
        if binding is None:
            return
        state = self.controller.state(binding.run_id)
        event.data["durable_audit"] = {
            "run_id": binding.run_id,
            "turn_id": binding.turn_id,
            "operation_id": state.current_operation_id,
        }

    async def model_during(self, event: HookEvent) -> None:
        binding = _RUN_BINDING.get()
        if binding is None:
            return
        await self._begin_model_attempt(event, binding)
        return
        state = self.controller.state(binding.run_id)
        if state.execution and state.execution.state is ExecutionState.OBSERVING:
            state = self.controller.apply(TransitionCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                execution_state=ExecutionState.THINKING,
                reason="工具结果已记录，开始下一次模型调用",
            )).state
        request_hash = hashlib.sha256(
            f"{state.run_id}:{binding.turn_id}:{state.model_attempt + 1}:{uuid4().hex}".encode("utf-8"),
        ).hexdigest()
        result = self.controller.apply(BeginOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=uuid4().hex,
            turn_id=binding.turn_id,
            kind=OperationKind.MODEL,
            name="model_call",
            request_hash=request_hash,
            idempotency=ToolIdempotency.PURE,
        ))
        operation = result.operation
        assert operation is not None
        state = result.state
        started = self.controller.apply(StartOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=operation.operation_id,
            heartbeat_expires_at=_after_seconds(120),
        ))
        self._operation.set(operation.operation_id)
        _CURRENT_OPERATION.set(operation.operation_id)
        self._heartbeat.set(asyncio.create_task(
            self._heartbeat_loop(operation.operation_id, operation.run_id),
            name=f"model-heartbeat-{operation.operation_id}",
        ))
        # model_attempt 是普通指标，不产生伪 FSM transition。
        from Agent.state import UpdateStateMetadataCommand
        state = started.state
        self.controller.apply(UpdateStateMetadataCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            model_attempt=state.model_attempt + 1,
            last_progress_at=now_iso(),
        ))

    async def _begin_model_attempt(self, event: HookEvent, binding: DurableRunBinding) -> None:
        state = self.controller.state(binding.run_id)
        if state.execution and state.execution.state is ExecutionState.OBSERVING:
            state = self.controller.apply(TransitionCommand(
                command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch, execution_state=ExecutionState.THINKING,
                reason="开始下一次逻辑模型调用",
            )).state
        canonical = json.dumps({
            "model": event.data.get("model"),
            "messages": event.data.get("messages"),
            "tools": event.data.get("tools"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing_id = self._operation.get()
        if existing_id is None and state.current_operation_id:
            candidate = self.controller.operation(state.current_operation_id)
            if candidate.kind is OperationKind.MODEL and (
                candidate.status is OperationStatus.ABANDONED
                or (
                    candidate.status is OperationStatus.FAILED
                    and candidate.failure_kind is OperationFailureKind.RETRYABLE
                )
            ):
                if candidate.request_hash != request_hash:
                    self.controller.apply(TransitionCommand(
                        command_id=uuid4().hex, run_id=state.run_id,
                        expected_revision=state.revision, gateway_epoch=self.controller.gateway_epoch,
                        task_state=TaskState.RECOVERY_REQUIRED,
                        reason="恢复后模型请求 request_hash 变化，禁止作为同一 Operation 重放",
                    ))
                    raise RuntimeError("Model recovery request_hash mismatch")
                existing_id = candidate.operation_id
                self._operation.set(existing_id)
        model_call_id = uuid4().hex
        if existing_id is None:
            logical_id = uuid4().hex
            created = self.controller.apply(CreateOperationWithAttemptCommand(
                command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch, operation_id=uuid4().hex,
                attempt_id=uuid4().hex, turn_id=binding.turn_id, kind=OperationKind.MODEL,
                name="model_call", stable_key=f"model:{binding.turn_id}:{logical_id}",
                request_hash=request_hash, idempotency=ToolIdempotency.PURE, side_effecting=False,
                logical_model_call_id=logical_id, model_call_id=model_call_id,
                retry_policy_snapshot=self.retry_policy,
            ))
        else:
            operation = self.controller.operation(existing_id)
            created = self.controller.apply(BeginOperationAttemptCommand(
                command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch, operation_id=operation.operation_id,
                attempt_id=uuid4().hex, expected_latest_attempt_no=operation.latest_attempt_no,
                request_hash=request_hash, model_call_id=model_call_id,
            ))
        operation, attempt = created.operation, created.attempt
        assert operation is not None and attempt is not None
        started = self.controller.apply(StartOperationAttemptCommand(
            command_id=uuid4().hex, run_id=state.run_id,
            expected_revision=created.state.revision, gateway_epoch=self.controller.gateway_epoch,
            attempt_id=attempt.attempt_id, heartbeat_expires_at=_after_seconds(120),
        ))
        self._operation.set(operation.operation_id)
        _CURRENT_OPERATION.set(operation.operation_id)
        _CURRENT_ATTEMPT.set(attempt.attempt_id)
        self._heartbeat.set(asyncio.create_task(
            self._model_attempt_heartbeat(attempt.attempt_id, operation.run_id),
            name=f"model-heartbeat-{attempt.attempt_id}",
        ))
        from Agent.state import UpdateStateMetadataCommand
        self.controller.apply(UpdateStateMetadataCommand(
            command_id=uuid4().hex, run_id=state.run_id,
            expected_revision=started.state.revision, gateway_epoch=self.controller.gateway_epoch,
            model_attempt=attempt.attempt_no, last_progress_at=now_iso(),
        ))
        event.data.update({
            "logical_model_call_id": operation.logical_model_call_id,
            "model_call_id": attempt.model_call_id,
            "attempt_no": attempt.attempt_no,
        })

    async def safe_checkpoint(self, event: HookEvent) -> None:
        binding = _RUN_BINDING.get()
        record_id = event.data.get("session_record_id")
        if binding is None or not isinstance(record_id, str) or not record_id:
            return
        state = self.controller.state(binding.run_id)
        operation = (
            self.controller.operation(state.current_operation_id)
            if state.current_operation_id
            else None
        )
        attempt = self.controller.current_attempt(operation.operation_id) if operation is not None else None
        if operation is not None and operation.status not in {
            OperationStatus.COMPLETED, OperationStatus.FAILED,
            OperationStatus.SKIPPED, OperationStatus.ABANDONED,
        }:
            return
        segment = self.memory.sessions.active_filename(event.session_id)
        checkpoint_id = hashlib.sha256(
            f"{state.run_id}:{record_id}:{state.revision}".encode("utf-8"),
        ).hexdigest()
        self.controller.apply(CreateSafeCheckpointCommand(
            command_id=f"checkpoint:{checkpoint_id}",
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            checkpoint=SafeCheckpoint(
                checkpoint_id=checkpoint_id,
                run_id=state.run_id,
                turn_id=binding.turn_id,
                state_revision=state.revision,
                last_determined_operation_id=operation.operation_id if operation else None,
                last_determined_attempt_id=attempt.attempt_id if attempt else None,
                current_operation_id=state.current_operation_id,
                current_attempt_id=state.current_attempt_id,
                session_segment=segment,
                last_record_id=record_id,
                created_at=now_iso(),
            ),
        ))

    async def model_after(self, event: HookEvent) -> None:
        operation_id = self._operation.get()
        binding = _RUN_BINDING.get()
        if operation_id is None or binding is None:
            return
        await self._finish_model_attempt(event, operation_id, binding)
        return
        heartbeat = self._heartbeat.get()
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._heartbeat.set(None)
        operation = self.controller.operation(operation_id)
        if operation.status is not OperationStatus.RUNNING:
            return
        state = self.controller.state(binding.run_id)
        event.data["durable_audit"] = {
            "run_id": binding.run_id,
            "turn_id": binding.turn_id,
            "operation_id": operation_id,
        }
        error = event.data.get("error")
        if error is not None:
            self.controller.apply(FailOperationCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                operation_id=operation_id,
                error=str(error) or type(error).__name__,
            ))
        else:
            metric = event.data.get("model_call")
            result = json.dumps(metric if isinstance(metric, dict) else {}, ensure_ascii=False, sort_keys=True)
            self.controller.apply(CompleteOperationCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                operation_id=operation_id,
                result=result,
                result_hash=hashlib.sha256(result.encode("utf-8")).hexdigest(),
                result_source="provider_response",
            ))
        self._operation.set(None)
        _CURRENT_OPERATION.set(None)

    async def _finish_model_attempt(
        self,
        event: HookEvent,
        operation_id: str,
        binding: DurableRunBinding,
    ) -> None:
        heartbeat = self._heartbeat.get()
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._heartbeat.set(None)
        attempt_id = _CURRENT_ATTEMPT.get()
        if attempt_id is None:
            return
        attempt = self.controller.current_attempt(operation_id)
        if attempt.attempt_id != attempt_id or attempt.status is not OperationStatus.RUNNING:
            return
        state = self.controller.state(binding.run_id)
        event.data["durable_audit"] = {
            "run_id": binding.run_id, "turn_id": binding.turn_id,
            "operation_id": operation_id, "attempt_id": attempt_id,
            "logical_model_call_id": self.controller.operation(operation_id).logical_model_call_id,
            "model_call_id": attempt.model_call_id, "attempt_no": attempt.attempt_no,
        }
        error = event.data.get("error")
        if error is not None:
            from Agent.models.errors import is_retryable_model_error
            failure_kind = (
                OperationFailureKind.RETRYABLE
                if is_retryable_model_error(error)
                else OperationFailureKind.TERMINAL
            )
            result = self.controller.apply(FailOperationAttemptCommand(
                command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch, attempt_id=attempt_id,
                failure_kind=failure_kind,
                failure_reason=str(error) or type(error).__name__,
            ))
            if result.operation is not None and result.operation.failure_kind is OperationFailureKind.TERMINAL:
                self._operation.set(None)
            _CURRENT_ATTEMPT.set(None)
            _CURRENT_OPERATION.set(None)
            return
        metric = event.data.get("model_call")
        serialized = json.dumps(metric if isinstance(metric, dict) else {}, ensure_ascii=False, sort_keys=True)
        self.controller.apply(CompleteOperationAttemptCommand(
            command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch, attempt_id=attempt_id,
            result=serialized, result_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            result_source="provider_response",
        ))
        self._operation.set(None)
        _CURRENT_OPERATION.set(None)
        _CURRENT_ATTEMPT.set(None)

    async def _model_attempt_heartbeat(self, attempt_id: str, run_id: str) -> None:
        while True:
            await asyncio.sleep(30)
            state = self.controller.state(run_id)
            timestamp = now_iso()
            try:
                self.controller.apply(HeartbeatOperationAttemptCommand(
                    command_id=uuid4().hex, run_id=run_id, expected_revision=state.revision,
                    gateway_epoch=self.controller.gateway_epoch, attempt_id=attempt_id,
                    heartbeat_at=timestamp, heartbeat_expires_at=_after_seconds(120),
                ))
            except (KeyError, RuntimeError):
                return

    async def _heartbeat_loop(self, operation_id: str, run_id: str) -> None:
        from Agent.state import HeartbeatOperationCommand
        while True:
            await asyncio.sleep(30)
            operation = self.controller.operation(operation_id)
            if operation.status is not OperationStatus.RUNNING:
                return
            state = self.controller.state(run_id)
            timestamp = now_iso()
            self.controller.apply(HeartbeatOperationCommand(
                command_id=uuid4().hex,
                run_id=run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                operation_id=operation_id,
                heartbeat_at=timestamp,
                heartbeat_expires_at=_after_seconds(120),
            ))


def _idempotency_of(tool: Any, risk: str) -> ToolIdempotency:
    declared = getattr(tool, "idempotency", None)
    if declared is not None:
        if isinstance(declared, ToolIdempotency):
            return declared
        try:
            return ToolIdempotency(str(declared))
        except ValueError as exc:
            raise ValueError(f"工具 {getattr(tool, 'name', '?')} 的 idempotency 声明无效") from exc
    if risk == "read":
        return ToolIdempotency.PURE
    if risk == "write":
        return ToolIdempotency.IDEMPOTENT
    return ToolIdempotency.NON_IDEMPOTENT


def _is_retryable_tool_error(error: BaseException) -> bool:
    """只重试明确可恢复的传输错误，避免对格式/内容错误做无意义重放。"""
    declared = getattr(error, "retryable", None)
    if isinstance(declared, bool):
        return declared
    return isinstance(error, (ConnectionError, TimeoutError, OSError))


def _after_seconds(seconds: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


__all__ = [
    "DurableModelHooks", "DurableToolCoordinator",
    "current_operation_id", "current_attempt_id",
]
