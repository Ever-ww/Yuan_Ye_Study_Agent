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
    HARNESS_EVOLUTION = "harness_evolution"
    HARNESS_DREAM = "harness_dream"
    MAINTENANCE = "maintenance"


class PersistenceContract(str, Enum):
    """Durable persistence responsibility fixed when a Run is created."""

    CONVERSATION_SESSION = "conversation_session"
    SESSION_BACKED_WORKLOAD = "session_backed_workload"
    CONTROL_ONLY = "control_only"


class OperationStatus(str, Enum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"
    ABANDONED = "abandoned"


class OperationFailureKind(str, Enum):
    TERMINAL = "terminal"
    RETRYABLE = "retryable"
    UNKNOWN_EFFECT = "unknown_effect"


class AttemptRecoveryResolution(str, Enum):
    UNRESOLVED = "unresolved"
    RETRY_AUTHORIZED = "retry_authorized"
    CONFIRMED_SUCCEEDED = "confirmed_succeeded"
    CONFIRMED_FAILED = "confirmed_failed"


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
    CRON_DISPATCH = "cron_dispatch"


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
        # OBSERVING is used for a policy-determined NOT_EXECUTED Tool result;
        # it must not detour through WAITING_HUMAN or pretend a side effect ran.
        ExecutionState.WAITING_HUMAN, ExecutionState.ACTING,
        ExecutionState.OBSERVING, ExecutionState.FINISHED,
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
    last_determined_attempt_id: str | None = None
    current_operation_id: str | None = None
    current_attempt_id: str | None = None
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
    persistence_contract: PersistenceContract = PersistenceContract.CONTROL_ONLY
    project_id: str = Field(min_length=1)
    session_id: str | None = None
    client_id: str = Field(min_length=1)
    parent_run_id: str | None = None
    idempotency_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_state: TaskState = TaskState.CREATED
    execution: ExecutionSnapshot | None = None
    terminal_target: TerminalTarget | None = None
    finalize_generation: int | None = Field(default=None, ge=1)
    turn_id: str | None = None
    current_operation_id: str | None = None
    current_attempt_id: str | None = None
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


class FinalizeGenerationRecord(BaseModel):
    """Immutable identity of one complete FINALIZING proof generation."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    protocol_version: Literal[2] = 2
    terminal_target: TerminalTarget
    persistence_contract: PersistenceContract
    created_at: str
    supersedes_generation: int | None = Field(default=None, ge=1)


class RetryPolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    max_attempts: int = Field(ge=1)
    base_seconds: float = Field(ge=0.0)
    max_seconds: float = Field(ge=0.0)
    automatic: bool = True
    requires_reconcile: bool = False
    requires_human_confirmation: bool = False


class ImmutableOperationMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    operation_id: str = Field(min_length=1)
    parent_operation_id: str | None = None
    run_id: str = Field(min_length=1)
    turn_id: str | None = None
    kind: OperationKind
    name: str = Field(min_length=1)
    stable_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency: ToolIdempotency = ToolIdempotency.NON_IDEMPOTENT
    side_effecting: bool = True
    logical_model_call_id: str | None = None
    source_model_call_id: str | None = None
    tool_call_id: str | None = None
    tool_batch_id: str | None = None
    tool_call_position: int | None = Field(default=None, ge=0)
    external_idempotency_key: str | None = None


class OperationAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    attempt_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    side_effecting: bool
    status: OperationStatus = OperationStatus.PREPARED
    failure_kind: OperationFailureKind | None = None
    failure_reason: str | None = None
    recovery_resolution: AttemptRecoveryResolution | None = None
    result: str | None = None
    result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_source: str | None = None
    skip_reason: str | None = None
    abandonment_reason: str | None = None
    started_at: str | None = None
    heartbeat_at: str | None = None
    heartbeat_expires_at: str | None = None
    completed_at: str | None = None
    model_call_id: str | None = None
    external_request_id: str | None = None
    reconcile_status: ReconcileStatus | None = None
    reconcile_evidence: str | None = None
    risk_confirmed_by: str | None = None
    risk_confirmation_reason: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_status_evidence(self) -> "OperationAttempt":
        if self.status is OperationStatus.FAILED:
            if self.failure_kind not in {OperationFailureKind.TERMINAL, OperationFailureKind.RETRYABLE}:
                raise ValueError("FAILED Attempt 必须声明 TERMINAL 或 RETRYABLE")
        elif self.status is OperationStatus.UNKNOWN:
            if self.failure_kind is not OperationFailureKind.UNKNOWN_EFFECT:
                raise ValueError("UNKNOWN Attempt 必须声明 UNKNOWN_EFFECT")
            if self.recovery_resolution is None:
                raise ValueError("UNKNOWN Attempt 必须包含 recovery_resolution")
        elif self.failure_kind is not None:
            raise ValueError("只有 FAILED/UNKNOWN Attempt 可以包含 failure_kind")
        if self.status is OperationStatus.SKIPPED:
            if self.result_source != "NOT_EXECUTED" or not self.skip_reason:
                raise ValueError("SKIPPED Attempt 必须包含 NOT_EXECUTED 与 skip_reason")
        elif self.skip_reason is not None:
            raise ValueError("只有 SKIPPED Attempt 可以包含 skip_reason")
        if self.status is OperationStatus.ABANDONED and not self.abandonment_reason:
            raise ValueError("ABANDONED Attempt 必须包含 abandonment_reason")
        if self.status is not OperationStatus.ABANDONED and self.abandonment_reason is not None:
            raise ValueError("只有 ABANDONED Attempt 可以包含 abandonment_reason")
        return self


class ToolObservationState(str, Enum):
    MATERIALIZED = "materialized"
    PUBLISHED = "published"


class MaterializedToolObservation(BaseModel):
    """Durable, ordered projection of one finalized Tool observation."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    operation_id: str | None = None
    attempt_id: str | None = None
    logical_model_call_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    position: int = Field(ge=0)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "error", "cancelled", "skipped"]
    finalized_content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ToolObservationState = ToolObservationState.MATERIALIZED
    session_record_id: str | None = None
    revision: int = Field(default=0, ge=0)
    created_at: str
    published_at: str | None = None


class OperationAggregate(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: OperationStatus
    failure_kind: OperationFailureKind | None = None
    failure_reason: str | None = None
    latest_attempt_no: int = Field(ge=1)
    next_retry_at: str | None = None
    result: str | None = None
    result_hash: str | None = None
    result_source: str | None = None
    skip_reason: str | None = None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> "OperationAggregate":
        if self.status is OperationStatus.FAILED:
            if self.failure_kind not in {
                OperationFailureKind.TERMINAL, OperationFailureKind.RETRYABLE,
            }:
                raise ValueError("FAILED Operation aggregate 必须声明 failure_kind")
        elif self.status is OperationStatus.UNKNOWN:
            if self.failure_kind is not OperationFailureKind.UNKNOWN_EFFECT:
                raise ValueError("UNKNOWN Operation aggregate 必须声明 UNKNOWN_EFFECT")
        elif self.failure_kind is not None:
            raise ValueError("非 FAILED/UNKNOWN Operation aggregate 不能携带 failure_kind")
        if self.status is OperationStatus.SKIPPED and not self.skip_reason:
            raise ValueError("SKIPPED Operation aggregate 必须包含 skip_reason")
        return self


def reduce_operation(
    metadata: ImmutableOperationMetadata,
    attempts: list[OperationAttempt] | tuple[OperationAttempt, ...],
    retry_policy: RetryPolicySnapshot,
) -> OperationAggregate:
    """仅由不可变元数据、Attempt 历史和策略快照生成聚合状态。"""

    if not attempts:
        raise ValueError("Logical Operation 必须至少包含一个 Attempt")
    ordered = tuple(sorted(attempts, key=lambda item: item.attempt_no))
    if tuple(item.attempt_no for item in ordered) != tuple(range(1, len(ordered) + 1)):
        raise ValueError("Attempt 编号必须从 1 开始连续递增")
    if any(
        item.operation_id != metadata.operation_id
        or item.run_id != metadata.run_id
        or item.request_hash != metadata.request_hash
        or item.side_effecting != metadata.side_effecting
        for item in ordered
    ):
        raise ValueError("Attempt 必须继承 Logical Operation 的身份、request_hash 和副作用属性")
    active = [
        item for item in ordered
        if item.status in {OperationStatus.PREPARED, OperationStatus.RUNNING}
        or (
            item.status is OperationStatus.UNKNOWN
            and item.recovery_resolution is AttemptRecoveryResolution.UNRESOLVED
        )
    ]
    if len(active) > 1:
        raise ValueError("Logical Operation 同时最多存在一个 active Attempt")
    latest = ordered[-1]
    if latest.status is OperationStatus.UNKNOWN:
        resolution = latest.recovery_resolution
        if resolution is AttemptRecoveryResolution.CONFIRMED_SUCCEEDED:
            return OperationAggregate(
                status=OperationStatus.COMPLETED, latest_attempt_no=latest.attempt_no,
                result=latest.result, result_hash=latest.result_hash,
                result_source=latest.result_source or "human_confirmed",
            )
        if resolution is AttemptRecoveryResolution.CONFIRMED_FAILED:
            return OperationAggregate(
                status=OperationStatus.FAILED, failure_kind=OperationFailureKind.TERMINAL,
                failure_reason=latest.failure_reason or "human_confirmed_failed",
                latest_attempt_no=latest.attempt_no,
            )
    if latest.status is OperationStatus.FAILED and latest.failure_kind is OperationFailureKind.RETRYABLE:
        if len(ordered) >= retry_policy.max_attempts:
            return OperationAggregate(
                status=OperationStatus.FAILED, failure_kind=OperationFailureKind.TERMINAL,
                failure_reason="retry_exhausted", latest_attempt_no=latest.attempt_no,
            )
        if not latest.completed_at:
            raise ValueError("RETRYABLE Attempt 必须包含 completed_at")
        completed = datetime.fromisoformat(latest.completed_at.replace("Z", "+00:00"))
        delay = min(retry_policy.max_seconds, retry_policy.base_seconds * (2 ** (latest.attempt_no - 1)))
        next_retry = completed.timestamp() + delay
        next_retry_at = datetime.fromtimestamp(next_retry, tz=completed.tzinfo).isoformat()
        return OperationAggregate(
            status=OperationStatus.FAILED, failure_kind=OperationFailureKind.RETRYABLE,
            failure_reason=latest.failure_reason, latest_attempt_no=latest.attempt_no,
            next_retry_at=next_retry_at,
        )
    return OperationAggregate(
        status=latest.status,
        failure_kind=latest.failure_kind,
        failure_reason=latest.failure_reason,
        latest_attempt_no=latest.attempt_no,
        result=latest.result,
        result_hash=latest.result_hash,
        result_source=latest.result_source,
        skip_reason=latest.skip_reason,
    )


class OperationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    operation_id: str = Field(min_length=1)
    parent_operation_id: str | None = None
    run_id: str = Field(min_length=1)
    turn_id: str | None = None
    kind: OperationKind
    name: str = Field(min_length=1)
    stable_key: str = Field(default="legacy", min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency: ToolIdempotency = ToolIdempotency.NON_IDEMPOTENT
    side_effecting: bool = True
    logical_model_call_id: str | None = None
    source_model_call_id: str | None = None
    tool_call_id: str | None = None
    tool_batch_id: str | None = None
    tool_call_position: int | None = Field(default=None, ge=0)
    external_idempotency_key: str | None = None
    retry_policy_snapshot: RetryPolicySnapshot = Field(
        default_factory=lambda: RetryPolicySnapshot(
            max_attempts=1, base_seconds=0.0, max_seconds=0.0,
            automatic=False, requires_reconcile=False, requires_human_confirmation=False,
        ),
    )
    status: OperationStatus = OperationStatus.PREPARED
    failure_kind: OperationFailureKind | None = None
    failure_reason: str | None = None
    latest_attempt_no: int = Field(default=1, ge=1)
    next_retry_at: str | None = None
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

    def immutable_metadata(self) -> ImmutableOperationMetadata:
        return ImmutableOperationMetadata(
            operation_id=self.operation_id, parent_operation_id=self.parent_operation_id,
            run_id=self.run_id, turn_id=self.turn_id, kind=self.kind, name=self.name,
            stable_key=self.stable_key, request_hash=self.request_hash,
            idempotency=self.idempotency, side_effecting=self.side_effecting,
            logical_model_call_id=self.logical_model_call_id,
            source_model_call_id=self.source_model_call_id, tool_call_id=self.tool_call_id,
            tool_batch_id=self.tool_batch_id, tool_call_position=self.tool_call_position,
            external_idempotency_key=self.external_idempotency_key,
        )


class DurableApproval(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    approval_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_no: int = Field(default=1, ge=1)
    stable_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
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
    # event_key 是同一 Command 内的稳定业务事件身份。默认 primary 保持现有
    # “一个命令产生一个主事件”的调用方式，同时允许事务内显式写入更多事件。
    event_key: str = Field(default="primary", min_length=1)
    causation_id: str | None = Field(default=None, min_length=1)
    correlation_id: str | None = Field(default=None, min_length=1)
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


class UpgradePersistenceContractCommand(StateCommand):
    persistence_contract: Literal[PersistenceContract.SESSION_BACKED_WORKLOAD]
    reason: str = Field(min_length=1)


class StartFinalizeGenerationCommand(StateCommand):
    generation: int = Field(ge=1)
    supersedes_generation: int | None = Field(default=None, ge=1)


class StartReplacementFinalizeGenerationCommand(StartFinalizeGenerationCommand):
    invalidated_generation: int = Field(ge=1)
    reason: str = Field(min_length=1)


class InvalidateFinalizeGenerationCommand(StateCommand):
    generation: int = Field(ge=1)
    reason: str = Field(min_length=1)


class FinalizeInboxCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    stable_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1)
    summary: str = ""
    status: Literal["completed", "failed", "cancelled"]


class FinalizeAuditCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    stable_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_id: str = Field(min_length=1)
    receipt_generation: int = Field(ge=1)
    receipt_json: str
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_json: str
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalizeTerminalCommand(StateCommand):
    """Atomically commits terminal State, projection, transition, event and outbox."""

    generation: int = Field(ge=1)
    reason: str = Field(default="FINALIZING completed", min_length=1)


class BeginOperationCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    parent_operation_id: str | None = None
    turn_id: str | None = None
    kind: OperationKind
    name: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency: ToolIdempotency


class CreateOperationWithAttemptCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    parent_operation_id: str | None = None
    turn_id: str | None = None
    kind: OperationKind
    name: str = Field(min_length=1)
    stable_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency: ToolIdempotency
    side_effecting: bool
    logical_model_call_id: str | None = None
    source_model_call_id: str | None = None
    tool_call_id: str | None = None
    tool_batch_id: str | None = None
    tool_call_position: int | None = Field(default=None, ge=0)
    model_call_id: str | None = None
    external_request_id: str | None = None
    external_idempotency_key: str | None = None
    retry_policy_snapshot: RetryPolicySnapshot


class BeginOperationAttemptCommand(StateCommand):
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    expected_latest_attempt_no: int = Field(ge=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_call_id: str | None = None
    external_request_id: str | None = None
    risk_confirmed_by: str | None = None
    risk_confirmation_reason: str | None = None


class StartOperationAttemptCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    heartbeat_expires_at: str | None = None


class CompleteOperationAttemptCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    result: str
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_source: str = Field(min_length=1)


class FailOperationAttemptCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    failure_kind: OperationFailureKind
    failure_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_failure(self) -> "FailOperationAttemptCommand":
        if self.failure_kind is OperationFailureKind.UNKNOWN_EFFECT:
            raise ValueError("UNKNOWN_EFFECT 必须使用 MarkOperationAttemptUnknownCommand")
        return self


class MarkOperationAttemptUnknownCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    failure_reason: str = Field(min_length=1)


class SkipOperationAttemptCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    skip_reason: str = Field(min_length=1)


class AbandonOperationAttemptCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    abandonment_reason: str = Field(min_length=1)


class HeartbeatOperationAttemptCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    heartbeat_at: str
    heartbeat_expires_at: str


class ReconcileOperationAttemptCommand(StateCommand):
    attempt_id: str = Field(min_length=1)
    result: ReconcileResult


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
    failure_kind: OperationFailureKind = OperationFailureKind.TERMINAL


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
    | UpgradePersistenceContractCommand
    | StartFinalizeGenerationCommand
    | StartReplacementFinalizeGenerationCommand
    | InvalidateFinalizeGenerationCommand
    | FinalizeAuditCommand
    | FinalizeInboxCommand
    | FinalizeTerminalCommand
    | BeginOperationCommand
    | CreateOperationWithAttemptCommand
    | BeginOperationAttemptCommand
    | StartOperationAttemptCommand
    | CompleteOperationAttemptCommand
    | FailOperationAttemptCommand
    | MarkOperationAttemptUnknownCommand
    | SkipOperationAttemptCommand
    | AbandonOperationAttemptCommand
    | HeartbeatOperationAttemptCommand
    | ReconcileOperationAttemptCommand
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
    attempt: OperationAttempt | None = None
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
    attempt: OperationAttempt | None = None,
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
        if operation is None or attempt is None:
            return False
        if attempt.status is OperationStatus.PREPARED:
            return True
        if attempt.status in {
            OperationStatus.COMPLETED, OperationStatus.FAILED,
            OperationStatus.SKIPPED, OperationStatus.ABANDONED,
        }:
            return True
        if attempt.status is OperationStatus.UNKNOWN:
            return False
        if attempt.status is OperationStatus.RUNNING:
            # Expiry only makes the attempt eligible for Recovery/reconcile.  It
            # never makes the original side effect dispatchable again.
            return False
    if operation and operation.status is OperationStatus.FAILED:
        if operation.failure_kind is not OperationFailureKind.RETRYABLE:
            return True
        if not operation.next_retry_at or not operation.retry_policy_snapshot.automatic:
            return False
        retry_at = datetime.fromisoformat(operation.next_retry_at.replace("Z", "+00:00"))
        selected = now if now.tzinfo is not None else now.astimezone()
        return selected >= retry_at
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
    "AbandonOperationAttemptCommand", "AbandonOperationCommand", "AdoptGatewayEpochCommand", "AgentState",
    "ApplyResult", "ApprovalStatus", "AttemptRecoveryResolution",
    "BeginOperationAttemptCommand",
    "BeginOperationCommand", "BindSessionCommand", "Command", "CompleteOperationCommand", "CreateSafeCheckpointCommand",
    "CompleteOperationAttemptCommand", "CreateOperationWithAttemptCommand",
    "CreateApprovalCommand", "DecideApprovalCommand", "DurableApproval",
    "ExecutionOutcome", "ExecutionSnapshot", "ExecutionState", "ExpireApprovalCommand",
    "FailOperationAttemptCommand", "FailOperationCommand", "FinalizeAuditCommand", "FinalizeGenerationRecord", "FinalizeInboxCommand",
    "FinalizeTerminalCommand",
    "HeartbeatOperationAttemptCommand", "HeartbeatOperationCommand", "ImmutableOperationMetadata", "INNER_TRANSITIONS",
    "InvalidateFinalizeGenerationCommand", "MarkOperationUnknownCommand", "OperationKind", "OperationRecord", "OperationStatus",
    "MarkOperationAttemptUnknownCommand", "MaterializedToolObservation", "OperationAggregate", "OperationAttempt", "OperationFailureKind",
    "OUTER_TRANSITIONS", "ReconcileOperationCommand", "ReconcileResult", "ReconcileStatus", "RecoveryDecisionCommand",
    "RecordRuntimeEventCommand", "ReconcileOperationAttemptCommand", "RequestCancellationCommand",
    "PersistenceContract", "RetryPolicySnapshot", "SafeCheckpoint", "SkipOperationAttemptCommand", "StartFinalizeGenerationCommand", "StartOperationAttemptCommand",
    "StartReplacementFinalizeGenerationCommand",
    "StartOperationCommand", "StateCommand",
    "TaskState", "TerminalTarget", "ToolIdempotency", "ToolObservationState", "TransitionCommand",
    "UpdateStateMetadataCommand", "UpgradePersistenceContractCommand", "WorkloadKind", "is_runnable", "projected_run_status",
    "reduce_operation", "validate_inner_transition", "validate_outer_transition",
]
