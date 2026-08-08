"""Durable Agent 控制面状态、显式 FSM 与统一命令契约。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    RECOVERING = "recovering"
    RECOVERY_REQUIRED = "recovery_required"
    CANCELLING = "cancelling"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ExecutionState(str, Enum):
    THINKING = "thinking"
    WAITING_HUMAN = "waiting_human"
    ACTING = "acting"
    OBSERVING = "observing"
    FINISHED = "finished"


class ExecutionOutcome(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"


class TerminalTarget(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkloadKind(str, Enum):
    CHAT = "chat"
    CRON = "cron"
    DREAM = "dream"
    DREAM_BACKFILL = "dream_backfill"
    DREAM_ROLLBACK = "dream_rollback"
    CODE_SESSION_START = "code_session_start"
    CODE_TURN = "code_turn"
    CODE_FINALIZE = "code_finalize"
    CODE_ABORT = "code_abort"
    MAINTENANCE = "maintenance"


class OperationStatus(str, Enum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    ABANDONED = "abandoned"


class OperationKind(str, Enum):
    MODEL = "model"
    TOOL = "tool"
    APPROVAL = "approval"
    MEMORY = "memory"
    SESSION_INDEX = "session_index"
    INBOX = "inbox"
    EVENT = "event"
    AUDIT = "audit"
    FINALIZE = "finalize"
    SUBAGENT = "subagent"


class ToolIdempotency(str, Enum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    EXTERNALLY_IDEMPOTENT = "externally_idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


class ReconcileStatus(str, Enum):
    COMPLETED = "completed"
    NOT_APPLIED = "not_applied"
    FAILED = "failed"
    UNKNOWN = "unknown"


TERMINAL_STATES = frozenset({
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.INTERRUPTED,
})

OUTER_TRANSITIONS = MappingProxyType({
    TaskState.CREATED: frozenset({TaskState.QUEUED, TaskState.FINALIZING}),
    TaskState.QUEUED: frozenset({TaskState.STARTING, TaskState.CANCELLING, TaskState.FINALIZING}),
    TaskState.STARTING: frozenset({
        TaskState.RUNNING, TaskState.RECOVERING, TaskState.CANCELLING,
        TaskState.FINALIZING, TaskState.INTERRUPTED,
    }),
    TaskState.RUNNING: frozenset({
        TaskState.RECOVERING, TaskState.RECOVERY_REQUIRED, TaskState.CANCELLING,
        TaskState.FINALIZING, TaskState.INTERRUPTED,
    }),
    TaskState.RECOVERING: frozenset({
        TaskState.QUEUED, TaskState.STARTING, TaskState.RUNNING,
        TaskState.RECOVERY_REQUIRED, TaskState.CANCELLING,
        TaskState.FINALIZING, TaskState.INTERRUPTED,
    }),
    TaskState.RECOVERY_REQUIRED: frozenset({
        TaskState.RECOVERING, TaskState.CANCELLING, TaskState.FINALIZING,
    }),
    TaskState.CANCELLING: frozenset({TaskState.RECOVERY_REQUIRED, TaskState.FINALIZING}),
    TaskState.FINALIZING: frozenset({
        TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
        TaskState.RECOVERING, TaskState.RECOVERY_REQUIRED, TaskState.INTERRUPTED,
    }),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.INTERRUPTED: frozenset(),
})

INNER_TRANSITIONS = MappingProxyType({
    ExecutionState.THINKING: frozenset({
        ExecutionState.WAITING_HUMAN, ExecutionState.ACTING, ExecutionState.FINISHED,
    }),
    ExecutionState.WAITING_HUMAN: frozenset({
        ExecutionState.ACTING, ExecutionState.OBSERVING, ExecutionState.FINISHED,
    }),
    ExecutionState.ACTING: frozenset({ExecutionState.OBSERVING, ExecutionState.FINISHED}),
    ExecutionState.OBSERVING: frozenset({ExecutionState.THINKING, ExecutionState.FINISHED}),
    ExecutionState.FINISHED: frozenset(),
})


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    parent: Literal["executing"] = "executing"
    state: ExecutionState
    outcome: ExecutionOutcome | None = None
    finish_reason: str | None = None
    entered_at: str

    @model_validator(mode="after")
    def validate_finished(self) -> "ExecutionSnapshot":
        if self.state is ExecutionState.FINISHED:
            if self.outcome is None or not self.finish_reason:
                raise ValueError("FINISHED 必须包含 outcome 与 finish_reason")
        elif self.outcome is not None or self.finish_reason is not None:
            raise ValueError("只有 FINISHED 可以包含 outcome 与 finish_reason")
        return self


class SafeCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    checkpoint_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    turn_id: str | None = None
    state_revision: int = Field(ge=0)
    last_determined_operation_id: str | None = None
    session_segment: str = Field(min_length=1)
    last_record_id: str = Field(min_length=1)
    created_at: str


class AgentState(BaseModel):
    """每个 Run 唯一的不可变控制面快照。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    gateway_epoch: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    workload_kind: WorkloadKind
    project_id: str = Field(min_length=1)
    session_id: str | None = None
    client_id: str = Field(min_length=1)
    parent_run_id: str | None = None
    idempotency_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_state: TaskState = TaskState.CREATED
    execution: ExecutionSnapshot | None = None
    terminal_target: TerminalTarget | None = None
    turn_id: str | None = None
    current_operation_id: str | None = None
    model_call_id: str | None = None
    tool_call_id: str | None = None
    approval_id: str | None = None
    safe_checkpoint: SafeCheckpoint | None = None
    cancellation_requested: bool = False
    deadline_at: str | None = None
    recovery_reason: str | None = None
    model_attempt: int = Field(default=0, ge=0)
    recovery_attempt: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    last_progress_at: str
    operation_started_at: str | None = None
    operation_heartbeat_at: str | None = None
    created_at: str
    started_at: str | None = None
    updated_at: str
    finished_at: str | None = None
    result_summary: str | None = None
    error: str | None = None

    @property
    def recovery_required(self) -> bool:
        return self.task_state is TaskState.RECOVERY_REQUIRED

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "AgentState":
        if self.task_state is TaskState.FINALIZING and self.terminal_target is None:
            raise ValueError("FINALIZING 必须指定 terminal_target")
        if self.task_state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
            if self.terminal_target is None or self.terminal_target.value != self.task_state.value:
                raise ValueError("正常终态必须匹配 terminal_target")
            if self.finished_at is None:
                raise ValueError("终态必须包含 finished_at")
        if self.task_state in TERMINAL_STATES and self.task_state is not TaskState.INTERRUPTED:
            if self.execution is not None and self.execution.state is not ExecutionState.FINISHED:
                raise ValueError("Agent Run 进入终态前内层必须 FINISHED")
        return self


class OperationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    operation_id: str = Field(min_length=1)
    parent_operation_id: str | None = None
    run_id: str = Field(min_length=1)
    turn_id: str | None = None
    kind: OperationKind
    name: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency: ToolIdempotency = ToolIdempotency.NON_IDEMPOTENT
    status: OperationStatus = OperationStatus.PREPARED
    result: str | None = None
    result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_source: str | None = None
    unknown_reason: str | None = None
    reconcile_status: ReconcileStatus | None = None
    reconcile_evidence: str | None = None
    checkpoint_id: str | None = None
    attempt: int = Field(default=0, ge=0)
    started_at: str | None = None
    heartbeat_at: str | None = None
    heartbeat_expires_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class DurableApproval(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    approval_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments_json: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str
    expires_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    reason: str | None = None


class ReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: ReconcileStatus
    evidence: str = ""
    result_source: str = "tool_reconcile"
    observed_result: str | None = None
    checked_at: str


class StateCommand(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    command_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    gateway_epoch: str = Field(min_length=1)


class TransitionCommand(StateCommand):
    task_state: TaskState | None = None
    execution_state: ExecutionState | None = None
    outcome: ExecutionOutcome | None = None
    finish_reason: str | None = None
    terminal_target: TerminalTarget | None = None
    reason: str = Field(min_length=1)
    error: str | None = None
    result_summary: str | None = None

    @model_validator(mode="after")
    def has_transition(self) -> "TransitionCommand":
        if self.task_state is None and self.execution_state is None:
            raise ValueError("TransitionCommand 必须改变外层或内层 FSM")
        if self.execution_state is ExecutionState.FINISHED:
            if self.outcome is None or not self.finish_reason:
                raise ValueError("迁移到 FINISHED 必须包含 outcome 与 finish_reason")
        elif self.outcome is not None or self.finish_reason is not None:
            raise ValueError("outcome/finish_reason 只能用于 FINISHED")
        return self


class UpdateStateMetadataCommand(StateCommand):
    model_attempt: int | None = Field(default=None, ge=0)
    recovery_attempt: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    last_progress_at: str | None = None
    diagnostics: dict[str, Any] | None = None


class RecordRuntimeEventCommand(StateCommand):
    event_type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    mark_progress: bool = False


class BindSessionCommand(StateCommand):
    session_id: str = Field(min_length=1)


class FinalizeInboxCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = ""
    status: Literal["completed", "failed", "cancelled"]


class BeginOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    parent_operation_id: str | None = None
    turn_id: str | None = None
    kind: OperationKind
    name: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency: ToolIdempotency


class StartOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    heartbeat_expires_at: str | None = None


class CompleteOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    result: str
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_source: str = Field(min_length=1)


class FailOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    error: str = Field(min_length=1)


class MarkOperationUnknownCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    unknown_reason: str = Field(min_length=1)


class AbandonOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class HeartbeatOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    heartbeat_at: str
    heartbeat_expires_at: str


class ReconcileOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    result: ReconcileResult


class CreateApprovalCommand(StateCommand):
    approval: DurableApproval


class DecideApprovalCommand(StateCommand):
    approval_id: str = Field(min_length=1)
    approved: bool
    decided_by: str = Field(min_length=1)
    reason: str = ""


class ExpireApprovalCommand(StateCommand):
    approval_id: str = Field(min_length=1)


class RequestCancellationCommand(StateCommand):
    reason: str = Field(min_length=1)


class CreateSafeCheckpointCommand(StateCommand):
    checkpoint: SafeCheckpoint


class RecoveryDecisionCommand(StateCommand):
    action: Literal["retry", "confirm_succeeded", "fail", "cancel"]
    operation_id: str | None = None
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    observed_result: str | None = None
    risk_confirmed: bool = False

    @model_validator(mode="after")
    def validate_recovery(self) -> "RecoveryDecisionCommand":
        if self.action == "confirm_succeeded" and not self.observed_result:
            raise ValueError("confirm_succeeded 必须提供 observed_result")
        return self


class AdoptGatewayEpochCommand(StateCommand):
    previous_gateway_epoch: str = Field(min_length=1)
    reason: str = Field(min_length=1)


Command = (
    TransitionCommand
    | UpdateStateMetadataCommand
    | RecordRuntimeEventCommand
    | BindSessionCommand
    | FinalizeInboxCommand
    | BeginOperationCommand
    | StartOperationCommand
    | CompleteOperationCommand
    | FailOperationCommand
    | MarkOperationUnknownCommand
    | AbandonOperationCommand
    | HeartbeatOperationCommand
    | ReconcileOperationCommand
    | CreateApprovalCommand
    | DecideApprovalCommand
    | ExpireApprovalCommand
    | RequestCancellationCommand
    | CreateSafeCheckpointCommand
    | RecoveryDecisionCommand
    | AdoptGatewayEpochCommand
)


class ApplyResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    state: AgentState
    operation: OperationRecord | None = None
    approval: DurableApproval | None = None
    event_id: str | None = None
    duplicate: bool = False


def validate_outer_transition(source: TaskState, target: TaskState) -> None:
    if target not in OUTER_TRANSITIONS[source]:
        raise ValueError(f"非法外层 FSM 迁移：{source.value} -> {target.value}")


def validate_inner_transition(source: ExecutionState, target: ExecutionState) -> None:
    if target not in INNER_TRANSITIONS[source]:
        raise ValueError(f"非法内层 FSM 迁移：{source.value} -> {target.value}")


def is_runnable(
    state: AgentState,
    operation: OperationRecord | None,
    *,
    now: datetime,
) -> bool:
    """所有调度器共同使用的、无副作用的可运行判定。"""
    if state.task_state in TERMINAL_STATES | {TaskState.RECOVERY_REQUIRED}:
        return False
    if state.diagnostics.get("fencing_token_valid") is False:
        return False
    if state.deadline_at and not state.cancellation_requested:
        deadline = datetime.fromisoformat(state.deadline_at.replace("Z", "+00:00"))
        selected = now if now.tzinfo is not None else now.astimezone()
        if selected >= deadline:
            return False
    if state.execution and state.execution.state is ExecutionState.WAITING_HUMAN:
        return False
    if state.execution and state.execution.state is ExecutionState.ACTING:
        if operation is None:
            return False
        if operation.status in {OperationStatus.PREPARED, OperationStatus.COMPLETED, OperationStatus.FAILED}:
            return True
        if operation.status in {OperationStatus.UNKNOWN, OperationStatus.ABANDONED}:
            return False
        if operation.status is OperationStatus.RUNNING:
            if not operation.heartbeat_expires_at:
                return False
            expires = datetime.fromisoformat(operation.heartbeat_expires_at.replace("Z", "+00:00"))
            selected = now if now.tzinfo is not None else now.astimezone()
            return selected >= expires
    return True


def projected_run_status(state: AgentState) -> str:
    if state.task_state in {TaskState.CREATED, TaskState.QUEUED}:
        return "queued"
    if state.task_state is TaskState.SUCCEEDED:
        return "completed"
    if state.task_state is TaskState.FAILED:
        return "failed"
    if state.task_state is TaskState.CANCELLED:
        return "cancelled"
    if state.task_state is TaskState.INTERRUPTED:
        return "interrupted"
    return "running"


__all__ = [
    "AbandonOperationCommand", "AdoptGatewayEpochCommand", "AgentState", "ApplyResult", "ApprovalStatus",
    "BeginOperationCommand", "BindSessionCommand", "Command", "CompleteOperationCommand", "CreateSafeCheckpointCommand",
    "CreateApprovalCommand", "DecideApprovalCommand", "DurableApproval",
    "ExecutionOutcome", "ExecutionSnapshot", "ExecutionState", "ExpireApprovalCommand",
    "FailOperationCommand", "FinalizeInboxCommand", "HeartbeatOperationCommand", "INNER_TRANSITIONS",
    "MarkOperationUnknownCommand", "OperationKind", "OperationRecord", "OperationStatus",
    "OUTER_TRANSITIONS", "ReconcileOperationCommand", "ReconcileResult", "ReconcileStatus", "RecoveryDecisionCommand",
    "RecordRuntimeEventCommand", "RequestCancellationCommand", "SafeCheckpoint", "StartOperationCommand", "StateCommand",
    "TaskState", "TerminalTarget", "ToolIdempotency", "TransitionCommand",
    "UpdateStateMetadataCommand", "WorkloadKind", "is_runnable", "projected_run_status",
    "validate_inner_transition", "validate_outer_transition",
]
