"""把 Tool Registry 接到 Durable Operation Ledger 的两阶段边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from Agent.state import (
    BeginOperationCommand,
    CompleteOperationCommand,
    CreateSafeCheckpointCommand,
    ExecutionOutcome,
    ExecutionState,
    FailOperationCommand,
    MarkOperationUnknownCommand,
    OperationKind,
    OperationRecord,
    OperationStatus,
    StartOperationCommand,
    SafeCheckpoint,
    TaskState,
    ToolIdempotency,
    TransitionCommand,
)
from Agent.hook import HookEvent, HookPoint, HookRegistry
from gateway.models import now_iso
from gateway.state_controller import StateController


@dataclass(frozen=True)
class DurableRunBinding:
    run_id: str
    turn_id: str


_RUN_BINDING: ContextVar[DurableRunBinding | None] = ContextVar("durable_run_binding", default=None)
_CURRENT_OPERATION: ContextVar[str | None] = ContextVar("durable_operation_id", default=None)


def current_operation_id() -> str | None:
    return _CURRENT_OPERATION.get()


class DurableToolCoordinator:
    """真实 Tool 副作用前后分别提交 Ledger，崩溃窗口保持 unknown。"""

    def __init__(self, controller: StateController, *, heartbeat_seconds: int = 60) -> None:
        self.controller = controller
        self.heartbeat_seconds = max(5, heartbeat_seconds)

    def bind(self, run_id: str, turn_id: str | None = None) -> Token:
        return _RUN_BINDING.set(DurableRunBinding(run_id, turn_id or uuid4().hex))

    def reset(self, token: Token) -> None:
        _RUN_BINDING.reset(token)

    async def prepare(self, *, tool: Any, name: str, arguments: dict[str, Any], risk: str) -> OperationRecord:
        binding = self._binding()
        parent_operation_id = _CURRENT_OPERATION.get()
        selected = _idempotency_of(tool, risk)
        serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
        result = self.controller.apply(BeginOperationCommand(
            command_id=uuid4().hex,
            run_id=binding.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=uuid4().hex,
            parent_operation_id=parent_operation_id,
            turn_id=binding.turn_id,
            kind=OperationKind.TOOL,
            name=name,
            request_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            idempotency=selected,
        ))
        assert result.operation is not None
        _CURRENT_OPERATION.set(result.operation.operation_id)
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

    async def approval_decided(self, operation: OperationRecord, *, approved: bool) -> None:
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

        expires = _after_seconds(self.heartbeat_seconds)
        started = self.controller.apply(StartOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=operation.operation_id,
            heartbeat_expires_at=expires,
        ))
        operation = started.operation or operation
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(operation),
            name=f"tool-heartbeat-{operation.operation_id}",
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
            await self._record_uncertain_or_failed(operation, exc, cancelled=False)
            raise
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        selected = str(result)
        state = self.controller.state(operation.run_id)
        self.controller.apply(CompleteOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=operation.operation_id,
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
        return selected

    async def _heartbeat_loop(self, operation: OperationRecord) -> None:
        from Agent.state import HeartbeatOperationCommand
        interval = max(2.0, self.heartbeat_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            current = self.controller.operation(operation.operation_id)
            if current.status is not OperationStatus.RUNNING:
                return
            state = self.controller.state(operation.run_id)
            timestamp = now_iso()
            self.controller.apply(HeartbeatOperationCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                operation_id=operation.operation_id,
                heartbeat_at=timestamp,
                heartbeat_expires_at=_after_seconds(self.heartbeat_seconds),
            ))

    async def preexecution_failed_if_safe(self, operation: OperationRecord | None, error: BaseException) -> None:
        if operation is None:
            return
        current = self.controller.operation(operation.operation_id)
        if current.status is not OperationStatus.PREPARED:
            return
        state = self.controller.state(operation.run_id)
        self.controller.apply(FailOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=operation.operation_id,
            error=str(error) or type(error).__name__,
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

    async def _record_uncertain_or_failed(
        self,
        operation: OperationRecord,
        error: BaseException,
        *,
        cancelled: bool,
    ) -> None:
        state = self.controller.state(operation.run_id)
        message = str(error) or type(error).__name__
        uncertain = operation.idempotency in {
            ToolIdempotency.EXTERNALLY_IDEMPOTENT,
            ToolIdempotency.NON_IDEMPOTENT,
        }
        if uncertain:
            self.controller.apply(MarkOperationUnknownCommand(
                command_id=uuid4().hex,
                run_id=state.run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                operation_id=operation.operation_id,
                unknown_reason=("cancelled during external operation: " if cancelled else "tool raised after dispatch: ") + message,
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
        self.controller.apply(FailOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=operation.operation_id,
            error=message,
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

    def __init__(self, controller: StateController, memory: Any | None = None) -> None:
        self.controller = controller
        self.memory = memory
        self._operation: ContextVar[str | None] = ContextVar("durable_model_operation", default=None)
        self._heartbeat: ContextVar[asyncio.Task[None] | None] = ContextVar("durable_model_heartbeat", default=None)

    def register(self, hooks: HookRegistry) -> None:
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
        del event
        binding = _RUN_BINDING.get()
        if binding is None:
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
        if operation is not None and operation.status not in {
            OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.ABANDONED,
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
        try:
            return ToolIdempotency(str(declared))
        except ValueError as exc:
            raise ValueError(f"工具 {getattr(tool, 'name', '?')} 的 idempotency 声明无效") from exc
    if risk == "read":
        return ToolIdempotency.PURE
    if risk == "write":
        return ToolIdempotency.IDEMPOTENT
    return ToolIdempotency.NON_IDEMPOTENT


def _after_seconds(seconds: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


__all__ = ["DurableModelHooks", "DurableToolCoordinator", "current_operation_id"]
