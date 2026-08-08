"""Gateway 启动时按 Durable State 恢复非终态 Run。"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from uuid import uuid4

from Agent.state import (
    AbandonOperationCommand,
    AdoptGatewayEpochCommand,
    ExecutionOutcome,
    ExecutionState,
    FinalizeInboxCommand,
    MarkOperationUnknownCommand,
    OperationKind,
    OperationStatus,
    TaskState,
    TerminalTarget,
    TransitionCommand,
    WorkloadKind,
)
from gateway.store import GatewayStore
from gateway.state_controller import StateController


EnqueueRun = Callable[[object], Awaitable[None]]


class RecoveryCoordinator:
    """不猜测未知副作用；可证明安全的 Run 才重新进入队列。"""

    def __init__(self, controller: StateController, store: GatewayStore, enqueue: EnqueueRun) -> None:
        self.controller = controller
        self.store = store
        self.enqueue = enqueue

    async def recover(self) -> dict[str, int]:
        counts = {"queued": 0, "waiting_human": 0, "recovery_required": 0, "finalized": 0}
        for stale in self.controller.nonterminal_states():
            state = stale
            if state.gateway_epoch != self.controller.gateway_epoch:
                state = self.controller.apply(AdoptGatewayEpochCommand(
                    command_id=uuid4().hex,
                    run_id=state.run_id,
                    expected_revision=state.revision,
                    gateway_epoch=self.controller.gateway_epoch,
                    previous_gateway_epoch=state.gateway_epoch,
                    reason="Gateway restart fencing takeover",
                )).state

            if state.task_state is TaskState.RECOVERY_REQUIRED:
                counts["recovery_required"] += 1
                continue
            if state.execution and state.execution.state is ExecutionState.WAITING_HUMAN:
                counts["waiting_human"] += 1
                continue
            if state.task_state is TaskState.FINALIZING:
                self._complete_finalizing(state)
                counts["finalized"] += 1
                continue
            if state.task_state is TaskState.CANCELLING:
                if self._has_unknown_effect(state.run_id):
                    self._transition(state.run_id, task=TaskState.RECOVERY_REQUIRED, reason="取消时副作用结果未知")
                    counts["recovery_required"] += 1
                else:
                    self._finish_cancel(state.run_id)
                    counts["finalized"] += 1
                continue
            if state.workload_kind not in {WorkloadKind.CHAT, WorkloadKind.CRON}:
                self._require_recovery(
                    state.run_id,
                    f"{state.workload_kind.value} workload 驱动在 Gateway 重启时已中断",
                )
                counts["recovery_required"] += 1
                continue
            if state.execution and state.execution.state is ExecutionState.FINISHED:
                self._finish_from_outcome(state.run_id, state.execution.outcome)
                counts["finalized"] += 1
                continue
            if state.execution and state.execution.state is ExecutionState.ACTING:
                if not self._recover_acting(state.run_id):
                    counts["recovery_required"] += 1
                    continue
                state = self.controller.state(state.run_id)

            if state.execution and state.execution.state is ExecutionState.THINKING:
                operation = self.controller.active_operation(state.run_id)
                if operation and operation.kind is OperationKind.MODEL and operation.status in {
                    OperationStatus.PREPARED, OperationStatus.RUNNING,
                }:
                    current = self.controller.state(state.run_id)
                    self.controller.apply(AbandonOperationCommand(
                        command_id=uuid4().hex,
                        run_id=current.run_id,
                        expected_revision=current.revision,
                        gateway_epoch=self.controller.gateway_epoch,
                        operation_id=operation.operation_id,
                        reason="Gateway 重启，旧模型 HTTP attempt 不可继续",
                    ))

            self._queue_for_recovery(state.run_id)
            await self.enqueue(self.store.run(state.run_id))
            counts["queued"] += 1
        return counts

    def _recover_acting(self, run_id: str) -> bool:
        state = self.controller.state(run_id)
        operation = (
            self.controller.operation(state.current_operation_id)
            if state.current_operation_id
            else None
        )
        if operation is None:
            self._require_recovery(run_id, "ACTING 缺少 Operation Ledger 记录")
            return False
        if operation.status in {OperationStatus.COMPLETED, OperationStatus.FAILED}:
            self._transition(run_id, execution=ExecutionState.OBSERVING, reason="恢复已确定的工具结果")
            return True
        if operation.status is OperationStatus.PREPARED:
            # 副作用确定尚未发出，但当前 React 栈已丢失；必须从 SafeCheckpoint 恢复。
            state = self.controller.state(run_id)
            if state.safe_checkpoint is None:
                self._require_recovery(run_id, "prepared Tool 缺少 SafeCheckpoint")
                return False
            return True
        if operation.status is OperationStatus.RUNNING:
            state = self.controller.state(run_id)
            self.controller.apply(MarkOperationUnknownCommand(
                command_id=uuid4().hex,
                run_id=run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                operation_id=operation.operation_id,
                unknown_reason="Gateway 在外部副作用执行期间退出",
            ))
        self._require_recovery(run_id, "工具副作用结果未知，必须 reconcile 或人工处理")
        return False

    def _queue_for_recovery(self, run_id: str) -> None:
        state = self.controller.state(run_id)
        if state.task_state is TaskState.CREATED:
            self._transition(run_id, task=TaskState.QUEUED, reason="恢复 CREATED Run")
            return
        if state.task_state is TaskState.QUEUED:
            return
        if state.task_state is not TaskState.RECOVERING:
            self._transition(run_id, task=TaskState.RECOVERING, reason="Gateway 重启恢复")
        self._transition(run_id, task=TaskState.QUEUED, reason="恢复边界校验完成，重新排队")

    def _require_recovery(self, run_id: str, reason: str) -> None:
        state = self.controller.state(run_id)
        if state.task_state is TaskState.RECOVERY_REQUIRED:
            return
        if state.task_state is not TaskState.RECOVERING:
            state = self._transition(run_id, task=TaskState.RECOVERING, reason="进入恢复检查")
        self._transition(run_id, task=TaskState.RECOVERY_REQUIRED, reason=reason)

    def _complete_finalizing(self, state) -> None:
        if state.terminal_target is None:
            self._transition(state.run_id, task=TaskState.INTERRUPTED, reason="FINALIZING 缺少 terminal_target")
            return
        self._ensure_inbox(state.run_id, state.terminal_target)
        state = self.controller.state(state.run_id)
        self._transition(state.run_id, task=TaskState(state.terminal_target.value), reason="重入 FINALIZING 完成")

    def _finish_cancel(self, run_id: str) -> None:
        state = self.controller.state(run_id)
        if state.execution and state.execution.state is not ExecutionState.FINISHED:
            self._transition(
                run_id, execution=ExecutionState.FINISHED,
                outcome=ExecutionOutcome.CANCELLED, finish_reason="恢复取消流程", reason="取消内层执行",
            )
        self._transition(
            run_id, task=TaskState.FINALIZING, terminal=TerminalTarget.CANCELLED,
            reason="恢复取消 FINALIZING",
        )
        self._ensure_inbox(run_id, TerminalTarget.CANCELLED)
        self._transition(run_id, task=TaskState.CANCELLED, reason="恢复取消完成")

    def _finish_from_outcome(self, run_id: str, outcome: ExecutionOutcome | None) -> None:
        target = {
            ExecutionOutcome.SUCCESS: TerminalTarget.SUCCEEDED,
            ExecutionOutcome.ERROR: TerminalTarget.FAILED,
            ExecutionOutcome.EXHAUSTED: TerminalTarget.FAILED,
            ExecutionOutcome.CANCELLED: TerminalTarget.CANCELLED,
        }.get(outcome)
        if target is None:
            state = self.controller.state(run_id)
            if state.task_state is TaskState.RUNNING:
                self._transition(run_id, task=TaskState.INTERRUPTED, reason="FINISHED outcome 损坏")
            return
        state = self.controller.state(run_id)
        if target is TerminalTarget.CANCELLED and state.task_state is not TaskState.CANCELLING:
            self._transition(run_id, task=TaskState.CANCELLING, reason="恢复取消路径")
        self._transition(run_id, task=TaskState.FINALIZING, terminal=target, reason="恢复 FINISHED Run")
        self._ensure_inbox(run_id, target)
        self._transition(run_id, task=TaskState(target.value), reason="恢复 FINALIZING 完成")

    def _ensure_inbox(self, run_id: str, target: TerminalTarget) -> None:
        state = self.controller.state(run_id)
        run = self.store.run(run_id)
        operation_id = hashlib.sha256(f"{run_id}:finalize:inbox".encode("utf-8")).hexdigest()
        try:
            operation = self.controller.operation(operation_id)
        except KeyError:
            operation = None
        if operation is not None and operation.status is OperationStatus.COMPLETED:
            return
        summary = state.result_summary or state.error or (
            state.execution.finish_reason if state.execution else ""
        )
        self.controller.apply(FinalizeInboxCommand(
            command_id=f"finalize:{run_id}:inbox",
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=operation_id,
            title=run.task[:120],
            summary=summary or "",
            status={
                TerminalTarget.SUCCEEDED: "completed",
                TerminalTarget.FAILED: "failed",
                TerminalTarget.CANCELLED: "cancelled",
            }[target],
        ))

    def _has_unknown_effect(self, run_id: str) -> bool:
        state = self.controller.state(run_id)
        operation = self.controller.operation(state.current_operation_id) if state.current_operation_id else None
        return operation is not None and operation.status in {OperationStatus.RUNNING, OperationStatus.UNKNOWN}

    def _transition(
        self,
        run_id: str,
        *,
        task: TaskState | None = None,
        execution: ExecutionState | None = None,
        outcome: ExecutionOutcome | None = None,
        finish_reason: str | None = None,
        terminal: TerminalTarget | None = None,
        reason: str,
    ):
        state = self.controller.state(run_id)
        return self.controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            task_state=task,
            execution_state=execution,
            outcome=outcome,
            finish_reason=finish_reason,
            terminal_target=terminal,
            reason=reason,
        )).state


__all__ = ["RecoveryCoordinator"]
