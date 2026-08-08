"""Durable Runtime 的唯一状态修改入口。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from Agent.state import (
    AbandonOperationCommand,
    AdoptGatewayEpochCommand,
    AgentState,
    ApplyResult,
    ApprovalStatus,
    BeginOperationCommand,
    BindSessionCommand,
    Command,
    CompleteOperationCommand,
    CreateApprovalCommand,
    CreateSafeCheckpointCommand,
    DecideApprovalCommand,
    DurableApproval,
    ExecutionOutcome,
    ExecutionSnapshot,
    ExecutionState,
    ExpireApprovalCommand,
    FailOperationCommand,
    FinalizeInboxCommand,
    HeartbeatOperationCommand,
    MarkOperationUnknownCommand,
    OperationKind,
    OperationRecord,
    OperationStatus,
    RecordRuntimeEventCommand,
    ReconcileOperationCommand,
    ReconcileStatus,
    RecoveryDecisionCommand,
    RequestCancellationCommand,
    StartOperationCommand,
    TaskState,
    TerminalTarget,
    ToolIdempotency,
    TransitionCommand,
    UpdateStateMetadataCommand,
    WorkloadKind,
    projected_run_status,
    validate_inner_transition,
    validate_outer_transition,
)
from gateway.audit import AuditSanitizer
from gateway.models import GatewayEventEnvelope, now_iso


class StateConflictError(RuntimeError):
    """revision、fencing 或命令内容冲突。"""


class StateInvariantError(RuntimeError):
    """命令违反 FSM、Operation 或 Checkpoint 不变量。"""


class StateController:
    """以 SQLite 事务实现 command 幂等、CAS、FSM guard 和 Outbox。"""

    SCHEMA_VERSION = 3

    def __init__(
        self,
        database_path: Path,
        *,
        gateway_epoch: str,
        migration_backup_path: Path | None = None,
    ) -> None:
        self.database_path = database_path.resolve()
        self.gateway_epoch = gateway_epoch
        self.migration_backup_path = migration_backup_path
        try:
            self.initialize()
        except Exception:
            if migration_backup_path is not None and migration_backup_path.exists():
                self._restore_backup(migration_backup_path)
            raise

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if check != "ok":
                raise RuntimeError(f"Gateway SQLite quick_check 失败：{check}")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_states (
                    run_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    gateway_epoch TEXT NOT NULL,
                    task_state TEXT NOT NULL,
                    execution_state TEXT,
                    recovery_required INTEGER NOT NULL DEFAULT 0,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state_transitions (
                    transition_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    from_task_state TEXT NOT NULL,
                    to_task_state TEXT NOT NULL,
                    from_execution_state TEXT,
                    to_execution_state TEXT,
                    reason TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_ledger (
                    operation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    parent_operation_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operation_ledger_run_idx
                    ON operation_ledger(run_id, status);
                CREATE TABLE IF NOT EXISTS durable_approvals (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS durable_approvals_run_idx
                    ON durable_approvals(run_id, status);
                CREATE TABLE IF NOT EXISTS processed_commands (
                    command_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gateway_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS event_outbox (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    eventbus_status TEXT NOT NULL DEFAULT 'pending',
                    jsonl_status TEXT NOT NULL DEFAULT 'pending',
                    eventbus_attempts INTEGER NOT NULL DEFAULT 0,
                    jsonl_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            final_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if final_check != "ok":
                raise RuntimeError(f"Gateway SQLite migration 后 quick_check 失败：{final_check}")

    def create_state(
        self,
        *,
        run_id: str,
        workload_kind: WorkloadKind,
        project_id: str,
        client_id: str,
        idempotency_key: str,
        request_hash: str,
        session_id: str | None = None,
        parent_run_id: str | None = None,
        deadline_at: str | None = None,
    ) -> AgentState:
        timestamp = now_iso()
        state = AgentState(
            gateway_epoch=self.gateway_epoch,
            run_id=run_id,
            workload_kind=workload_kind,
            project_id=project_id,
            session_id=session_id,
            client_id=client_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            deadline_at=deadline_at,
            last_progress_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT state_json FROM agent_states WHERE run_id=?", (run_id,),
            ).fetchone()
            if existing is not None:
                selected = AgentState.model_validate_json(existing["state_json"], strict=True)
                connection.commit()
                return selected
            connection.execute(
                "INSERT INTO agent_states VALUES(?,?,?,?,?,?,?,?)",
                self._state_row(state),
            )
            self._update_run_projection(connection, state)
            self._write_event(connection, state, "state_created", {"task_state": state.task_state.value})
            connection.commit()
        return state

    def create_run(
        self,
        *,
        run_id: str,
        workload_kind: WorkloadKind,
        project_id: str,
        client_id: str,
        task: str,
        idempotency_key: str,
        request_hash: str,
        session_id: str | None = None,
        parent_run_id: str | None = None,
        deadline_at: str | None = None,
    ) -> tuple[AgentState, bool]:
        """原子建立 Run 投影、幂等记录、AgentState、Event 与 Outbox。"""
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_hash,run_id FROM idempotency_records WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise StateConflictError("Idempotency-Key 已用于不同请求")
                state = self._state_in(connection, str(existing["run_id"]))
                connection.commit()
                return state, True
            state = AgentState(
                gateway_epoch=self.gateway_epoch,
                run_id=run_id,
                workload_kind=workload_kind,
                project_id=project_id,
                session_id=session_id,
                client_id=client_id,
                parent_run_id=parent_run_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                deadline_at=deadline_at,
                last_progress_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
            connection.execute(
                "INSERT INTO runs(run_id,project_id,session_id,client_id,task,status,created_at,"
                "started_at,finished_at,answer,error) VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL,NULL)",
                (run_id, project_id, session_id, client_id, task, timestamp),
            )
            connection.execute(
                "INSERT INTO event_sequences(run_id,last_sequence) VALUES(?,0)", (run_id,),
            )
            connection.execute("INSERT INTO agent_states VALUES(?,?,?,?,?,?,?,?)", self._state_row(state))
            connection.execute(
                "INSERT INTO idempotency_records VALUES(?,?,?,?,?,?)",
                (idempotency_key, request_hash, run_id, None, timestamp, timestamp),
            )
            self._update_run_projection(connection, state)
            self._write_event(connection, state, "state_created", {"task_state": state.task_state.value})
            connection.commit()
            return state, False

    def apply(self, command: Command) -> ApplyResult:
        command_json = command.model_dump_json()
        command_hash = hashlib.sha256(command_json.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT command_hash,result_json FROM processed_commands WHERE command_id=?",
                (command.command_id,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["command_hash"] != command_hash:
                    raise StateConflictError("同一 command_id 被用于不同命令")
                result = ApplyResult.model_validate_json(duplicate["result_json"], strict=True)
                connection.commit()
                return result.model_copy(update={"duplicate": True})

            state = self._state_in(connection, command.run_id)
            if state.revision != command.expected_revision:
                raise StateConflictError(
                    f"State revision 冲突：期望 {command.expected_revision}，实际 {state.revision}",
                )
            adopting = isinstance(command, AdoptGatewayEpochCommand)
            if command.gateway_epoch != self.gateway_epoch:
                raise StateConflictError("Gateway epoch/fencing token 不匹配")
            if adopting:
                if command.previous_gateway_epoch != state.gateway_epoch:
                    raise StateConflictError("待接管的 Gateway epoch 已变化")
            elif state.gateway_epoch != self.gateway_epoch:
                raise StateConflictError("旧 Gateway 实例无权继续写入")

            previous = state
            operation: OperationRecord | None = None
            approval: DurableApproval | None = None
            transition = False
            timestamp = now_iso()

            if isinstance(command, AdoptGatewayEpochCommand):
                state = state.model_copy(update={
                    "gateway_epoch": self.gateway_epoch,
                    "diagnostics": {
                        **state.diagnostics,
                        "last_epoch_adoption": {"from": command.previous_gateway_epoch, "at": timestamp},
                    },
                })
            elif isinstance(command, TransitionCommand):
                state = self._apply_transition(state, command, timestamp)
                transition = True
            elif isinstance(command, UpdateStateMetadataCommand):
                updates = {
                    key: value
                    for key, value in command.model_dump().items()
                    if key in {"model_attempt", "recovery_attempt", "input_tokens", "output_tokens", "last_progress_at"}
                    and value is not None
                }
                if command.diagnostics is not None:
                    updates["diagnostics"] = {
                        **state.diagnostics,
                        **AuditSanitizer.sanitize(command.diagnostics),
                    }
                state = state.model_copy(update=updates)
            elif isinstance(command, RecordRuntimeEventCommand):
                if command.mark_progress:
                    state = state.model_copy(update={"last_progress_at": timestamp})
            elif isinstance(command, BindSessionCommand):
                if state.session_id is not None and state.session_id != command.session_id:
                    raise StateConflictError("Run 已绑定到另一个 Session")
                state = state.model_copy(update={"session_id": command.session_id})
            elif isinstance(command, FinalizeInboxCommand):
                if state.task_state is not TaskState.FINALIZING:
                    raise StateInvariantError("Inbox 只能在 FINALIZING 中生成")
                item_id = hashlib.sha256(f"{state.run_id}:finalize:inbox".encode("utf-8")).hexdigest()[:32]
                connection.execute(
                    "INSERT OR IGNORE INTO inbox(item_id,run_id,project_id,session_id,title,summary,status,created_at,is_read) "
                    "VALUES(?,?,?,?,?,?,?,?,0)",
                    (
                        item_id, state.run_id, state.project_id, state.session_id,
                        command.title[:120], AuditSanitizer.sanitize(command.summary), command.status, timestamp,
                    ),
                )
                request_hash = hashlib.sha256(
                    f"{state.run_id}:{command.status}:{command.summary}".encode("utf-8"),
                ).hexdigest()
                operation = OperationRecord(
                    operation_id=command.operation_id,
                    run_id=state.run_id,
                    turn_id=state.turn_id,
                    kind=OperationKind.INBOX,
                    name="finalize_inbox",
                    request_hash=request_hash,
                    idempotency=ToolIdempotency.IDEMPOTENT,
                    status=OperationStatus.COMPLETED,
                    result=item_id,
                    result_hash=hashlib.sha256(item_id.encode("utf-8")).hexdigest(),
                    result_source="sqlite_transaction",
                    started_at=timestamp,
                    completed_at=timestamp,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            elif isinstance(command, BeginOperationCommand):
                operation = self._begin_operation(connection, state, command, timestamp)
                state = state.model_copy(update={
                    "current_operation_id": operation.operation_id,
                    "turn_id": command.turn_id or state.turn_id,
                    **({"model_call_id": operation.operation_id} if command.kind is OperationKind.MODEL else {}),
                    **({"tool_call_id": operation.operation_id} if command.kind is OperationKind.TOOL else {}),
                })
            elif isinstance(command, StartOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.status is not OperationStatus.PREPARED:
                    raise StateInvariantError("只有当前 Run 的 prepared Operation 可以启动")
                operation = operation.model_copy(update={
                    "status": OperationStatus.RUNNING,
                    "attempt": operation.attempt + 1,
                    "started_at": timestamp,
                    "heartbeat_at": timestamp,
                    "heartbeat_expires_at": command.heartbeat_expires_at,
                    "updated_at": timestamp,
                })
                state = state.model_copy(update={
                    "operation_started_at": timestamp,
                    "operation_heartbeat_at": timestamp,
                    "last_progress_at": timestamp,
                })
            elif isinstance(command, CompleteOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.status not in {OperationStatus.RUNNING, OperationStatus.UNKNOWN}:
                    raise StateInvariantError("只有 running/unknown Operation 可以完成")
                operation = operation.model_copy(update={
                    "status": OperationStatus.COMPLETED,
                    "result": AuditSanitizer.sanitize(command.result),
                    "result_hash": command.result_hash,
                    "result_source": command.result_source,
                    "unknown_reason": None,
                    "completed_at": timestamp,
                    "updated_at": timestamp,
                })
                state = state.model_copy(update={"last_progress_at": timestamp})
            elif isinstance(command, FailOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.status not in {
                    OperationStatus.PREPARED, OperationStatus.RUNNING, OperationStatus.UNKNOWN,
                }:
                    raise StateInvariantError("只有 prepared/running/unknown Operation 可以失败")
                operation = operation.model_copy(update={
                    "status": OperationStatus.FAILED,
                    "error": AuditSanitizer.sanitize(command.error),
                    "completed_at": timestamp,
                    "updated_at": timestamp,
                })
                state = state.model_copy(update={"last_progress_at": timestamp})
            elif isinstance(command, MarkOperationUnknownCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.status is not OperationStatus.RUNNING:
                    raise StateInvariantError("只有 running Operation 可以标记为 unknown")
                operation = operation.model_copy(update={
                    "status": OperationStatus.UNKNOWN,
                    "unknown_reason": AuditSanitizer.sanitize(command.unknown_reason),
                    "updated_at": timestamp,
                })
                state = state.model_copy(update={"recovery_reason": operation.unknown_reason})
            elif isinstance(command, AbandonOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.kind is not OperationKind.MODEL:
                    raise StateInvariantError("只有当前 Run 的模型 Operation 可以 abandoned")
                if operation.status in {OperationStatus.COMPLETED, OperationStatus.FAILED}:
                    raise StateInvariantError("已确定结果的 Operation 不能 abandoned")
                operation = operation.model_copy(update={
                    "status": OperationStatus.ABANDONED,
                    "error": AuditSanitizer.sanitize(command.reason),
                    "completed_at": timestamp,
                    "updated_at": timestamp,
                })
            elif isinstance(command, HeartbeatOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.status is not OperationStatus.RUNNING:
                    raise StateInvariantError("只能为 running Operation 更新 heartbeat")
                operation = operation.model_copy(update={
                    "heartbeat_at": command.heartbeat_at,
                    "heartbeat_expires_at": command.heartbeat_expires_at,
                    "updated_at": timestamp,
                })
                state = state.model_copy(update={"operation_heartbeat_at": command.heartbeat_at})
            elif isinstance(command, ReconcileOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.status not in {
                    OperationStatus.RUNNING, OperationStatus.UNKNOWN,
                }:
                    raise StateInvariantError("只能 reconcile running/unknown Operation")
                reconciled = command.result
                common = {
                    "reconcile_status": reconciled.status,
                    "reconcile_evidence": AuditSanitizer.sanitize(reconciled.evidence),
                    "result_source": reconciled.result_source,
                    "updated_at": timestamp,
                }
                if reconciled.status is ReconcileStatus.COMPLETED:
                    observed = reconciled.observed_result or ""
                    operation = operation.model_copy(update={
                        **common,
                        "status": OperationStatus.COMPLETED,
                        "result": AuditSanitizer.sanitize(observed),
                        "result_hash": hashlib.sha256(observed.encode("utf-8")).hexdigest(),
                        "completed_at": timestamp,
                        "unknown_reason": None,
                    })
                elif reconciled.status is ReconcileStatus.NOT_APPLIED:
                    operation = operation.model_copy(update={
                        **common, "status": OperationStatus.PREPARED,
                        "unknown_reason": None, "heartbeat_at": None, "heartbeat_expires_at": None,
                    })
                elif reconciled.status is ReconcileStatus.FAILED:
                    operation = operation.model_copy(update={
                        **common, "status": OperationStatus.FAILED,
                        "error": AuditSanitizer.sanitize(reconciled.evidence or "reconcile confirmed failure"),
                        "completed_at": timestamp,
                    })
                else:
                    operation = operation.model_copy(update={
                        **common, "status": OperationStatus.UNKNOWN,
                        "unknown_reason": AuditSanitizer.sanitize(reconciled.evidence or "reconcile inconclusive"),
                    })
            elif isinstance(command, CreateApprovalCommand):
                approval = command.approval
                if approval.run_id != state.run_id:
                    raise StateInvariantError("Approval 不属于当前 Run")
                connection.execute(
                    "INSERT INTO durable_approvals VALUES(?,?,?,?,?,?,?)",
                    (
                        approval.approval_id, approval.run_id, approval.operation_id,
                        approval.status.value, approval.model_dump_json(), approval.expires_at, timestamp,
                    ),
                )
                state = state.model_copy(update={"approval_id": approval.approval_id})
            elif isinstance(command, DecideApprovalCommand):
                approval = self._approval_in(connection, command.approval_id)
                if approval.run_id != state.run_id:
                    raise StateInvariantError("Approval 不属于当前 Run")
                desired = ApprovalStatus.APPROVED if command.approved else ApprovalStatus.DENIED
                if approval.status is not ApprovalStatus.PENDING and approval.status is not desired:
                    raise StateConflictError("Approval 已收到相反决定")
                if approval.status is ApprovalStatus.PENDING:
                    approval = approval.model_copy(update={
                        "status": desired,
                        "decided_at": timestamp,
                        "decided_by": command.decided_by,
                        "reason": AuditSanitizer.sanitize(command.reason),
                    })
            elif isinstance(command, ExpireApprovalCommand):
                approval = self._approval_in(connection, command.approval_id)
                if approval.run_id != state.run_id:
                    raise StateInvariantError("Approval 不属于当前 Run")
                if approval.status is ApprovalStatus.PENDING:
                    if datetime.fromisoformat(approval.expires_at) > datetime.fromisoformat(timestamp):
                        raise StateInvariantError("Approval 尚未到期")
                    approval = approval.model_copy(update={
                        "status": ApprovalStatus.TIMEOUT,
                        "decided_at": timestamp,
                        "decided_by": "gateway-timeout",
                        "reason": "approval expired",
                    })
            elif isinstance(command, RequestCancellationCommand):
                state = state.model_copy(update={
                    "cancellation_requested": True,
                    "recovery_reason": AuditSanitizer.sanitize(command.reason),
                })
            elif isinstance(command, CreateSafeCheckpointCommand):
                active = connection.execute(
                    "SELECT 1 FROM operation_ledger WHERE run_id=? AND kind='tool' "
                    "AND status IN ('running','unknown') LIMIT 1",
                    (state.run_id,),
                ).fetchone()
                if active is not None:
                    raise StateInvariantError("存在结果未确定的副作用，不能创建 SafeCheckpoint")
                if command.checkpoint.run_id != state.run_id or command.checkpoint.state_revision != state.revision:
                    raise StateInvariantError("SafeCheckpoint 的 Run 或 revision 不匹配")
                state = state.model_copy(update={"safe_checkpoint": command.checkpoint})
            elif isinstance(command, RecoveryDecisionCommand):
                state, operation = self._apply_recovery(connection, state, command, timestamp)
            else:  # pragma: no cover - Union 新增类型时强制实现
                raise TypeError(f"未实现的 State command：{type(command).__name__}")

            state = AgentState.model_validate(
                state.model_copy(update={"revision": state.revision + 1, "updated_at": timestamp}),
                strict=True,
            )
            if operation is not None:
                self._save_operation(connection, operation)
            if approval is not None:
                self._save_approval(connection, approval)

            cursor = connection.execute(
                "UPDATE agent_states SET revision=?,gateway_epoch=?,task_state=?,execution_state=?,"
                "recovery_required=?,state_json=?,updated_at=? WHERE run_id=? AND revision=?",
                (*self._state_row(state)[1:], state.run_id, previous.revision),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("State CAS 更新失败")
            self._update_run_projection(connection, state)
            if transition:
                connection.execute(
                    "INSERT INTO state_transitions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex, command.command_id, state.run_id,
                        previous.task_state.value, state.task_state.value,
                        previous.execution.state.value if previous.execution else None,
                        state.execution.state.value if state.execution else None,
                        command.reason, state.revision, timestamp,
                    ),
                )
            event = self._write_event(
                connection,
                state,
                self._event_type(command),
                self._event_payload(command, previous, state, operation, approval),
            )
            result = ApplyResult(state=state, operation=operation, approval=approval, event_id=event.event_id)
            connection.execute(
                "INSERT INTO processed_commands VALUES(?,?,?,?,?)",
                (command.command_id, state.run_id, command_hash, result.model_dump_json(), timestamp),
            )
            connection.commit()
            return result

    def state(self, run_id: str) -> AgentState:
        with self._connection() as connection:
            return self._state_in(connection, run_id)

    def operation(self, operation_id: str) -> OperationRecord:
        with self._connection() as connection:
            return self._operation_in(connection, operation_id)

    def active_operation(self, run_id: str) -> OperationRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM operation_ledger WHERE run_id=? "
                "AND status IN ('prepared','running','unknown') ORDER BY updated_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return OperationRecord.model_validate_json(row["record_json"], strict=True) if row else None

    def approval(self, approval_id: str) -> DurableApproval:
        with self._connection() as connection:
            return self._approval_in(connection, approval_id)

    def reserve_idempotency(self, key: str, request_hash: str, run_id: str) -> str:
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_hash,run_id FROM idempotency_records WHERE idempotency_key=?", (key,),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise StateConflictError("Idempotency-Key 已用于不同请求")
                connection.commit()
                return str(row["run_id"])
            connection.execute(
                "INSERT INTO idempotency_records VALUES(?,?,?,?,?,?)",
                (key, request_hash, run_id, None, timestamp, timestamp),
            )
            connection.commit()
        return run_id

    def events(self, run_id: str, after_sequence: int = 0) -> list[GatewayEventEnvelope]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT event_json FROM gateway_events WHERE run_id=? AND sequence>? ORDER BY sequence",
                (run_id, after_sequence),
            ).fetchall()
        return [GatewayEventEnvelope.model_validate_json(row["event_json"], strict=True) for row in rows]

    def nonterminal_states(self) -> list[AgentState]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT state_json FROM agent_states WHERE task_state NOT IN "
                "('succeeded','failed','cancelled','interrupted') ORDER BY updated_at,run_id",
            ).fetchall()
        return [AgentState.model_validate_json(row["state_json"], strict=True) for row in rows]

    def health(self) -> dict[str, Any]:
        timestamp = datetime.now().astimezone()
        with self._connection() as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            outbox = int(connection.execute(
                "SELECT COUNT(*) FROM event_outbox WHERE delivered_at IS NULL",
            ).fetchone()[0])
            recovery = int(connection.execute(
                "SELECT COUNT(*) FROM agent_states WHERE task_state='recovery_required'",
            ).fetchone()[0])
            pending = int(connection.execute(
                "SELECT COUNT(*) FROM durable_approvals WHERE status='pending' AND expires_at>?",
                (timestamp.isoformat(timespec="seconds"),),
            ).fetchone()[0])
            expired = int(connection.execute(
                "SELECT COUNT(*) FROM durable_approvals WHERE status='pending' AND expires_at<=?",
                (timestamp.isoformat(timespec="seconds"),),
            ).fetchone()[0])
            stalled = int(connection.execute(
                "SELECT COUNT(*) FROM operation_ledger WHERE status='running' AND "
                "json_extract(record_json,'$.heartbeat_expires_at') IS NOT NULL AND "
                "json_extract(record_json,'$.heartbeat_expires_at')<=?",
                (timestamp.isoformat(timespec="seconds"),),
            ).fetchone()[0])
            oldest_state = connection.execute(
                "SELECT MIN(updated_at) FROM agent_states WHERE task_state NOT IN "
                "('succeeded','failed','cancelled','interrupted')",
            ).fetchone()[0]
            oldest_heartbeat = connection.execute(
                "SELECT MIN(json_extract(record_json,'$.heartbeat_at')) FROM operation_ledger "
                "WHERE status='running'",
            ).fetchone()[0]
        return {
            "sqlite_quick_check": quick_check,
            "outbox_backlog": outbox,
            "recovery_required": recovery,
            "pending_approvals": pending,
            "expired_approvals": expired,
            "stalled_operations": stalled,
            "max_state_age_seconds": _age_seconds(oldest_state, timestamp),
            "max_heartbeat_age_seconds": _age_seconds(oldest_heartbeat, timestamp),
            "migration_backup": str(self.migration_backup_path) if self.migration_backup_path else None,
        }

    def _apply_transition(self, state: AgentState, command: TransitionCommand, timestamp: str) -> AgentState:
        updates: dict[str, Any] = {}
        if command.task_state is not None:
            validate_outer_transition(state.task_state, command.task_state)
            updates["task_state"] = command.task_state
            if command.task_state is TaskState.RUNNING and state.started_at is None:
                updates["started_at"] = timestamp
            if command.task_state is TaskState.FINALIZING:
                target = command.terminal_target or state.terminal_target
                if target is None:
                    raise StateInvariantError("进入 FINALIZING 必须指定 terminal_target")
                updates["terminal_target"] = target
            elif command.terminal_target is not None:
                updates["terminal_target"] = command.terminal_target
            if command.task_state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED}:
                updates["finished_at"] = timestamp
        if command.execution_state is not None:
            if state.execution is None:
                if command.execution_state is not ExecutionState.THINKING:
                    raise StateInvariantError("内层 FSM 必须从 THINKING 初始化")
            else:
                validate_inner_transition(state.execution.state, command.execution_state)
            updates["execution"] = ExecutionSnapshot(
                state=command.execution_state,
                outcome=command.outcome,
                finish_reason=command.finish_reason,
                entered_at=timestamp,
            )
        if command.error is not None:
            updates["error"] = AuditSanitizer.sanitize(command.error)
        if command.result_summary is not None:
            updates["result_summary"] = command.result_summary
        updates["last_progress_at"] = timestamp
        selected = state.model_copy(update=updates)
        self._validate_terminal_mapping(selected)
        return selected

    @staticmethod
    def _validate_terminal_mapping(state: AgentState) -> None:
        if state.task_state not in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
            return
        if state.execution is None:
            return
        outcome = state.execution.outcome
        expected = {
            ExecutionOutcome.SUCCESS: TaskState.SUCCEEDED,
            ExecutionOutcome.ERROR: TaskState.FAILED,
            ExecutionOutcome.EXHAUSTED: TaskState.FAILED,
            ExecutionOutcome.CANCELLED: TaskState.CANCELLED,
        }.get(outcome)
        if expected is not state.task_state:
            raise StateInvariantError(f"FINISHED outcome {outcome} 不能投影到 {state.task_state.value}")

    def _begin_operation(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: BeginOperationCommand,
        timestamp: str,
    ) -> OperationRecord:
        row = connection.execute(
            "SELECT record_json FROM operation_ledger WHERE operation_id=?", (command.operation_id,),
        ).fetchone()
        if row is not None:
            existing = OperationRecord.model_validate_json(row["record_json"], strict=True)
            if existing.run_id != state.run_id or existing.request_hash != command.request_hash:
                raise StateConflictError("operation_id 已用于不同操作")
            return existing
        if command.kind is OperationKind.TOOL and command.idempotency is not ToolIdempotency.PURE:
            active = connection.execute(
                "SELECT operation_id FROM operation_ledger WHERE run_id=? AND kind='tool' "
                "AND status IN ('prepared','running','unknown') LIMIT 1",
                (state.run_id,),
            ).fetchone()
            if active is not None:
                raise StateInvariantError("每个 Run 最多只能有一个 active side-effect operation")
        return OperationRecord(
            operation_id=command.operation_id,
            parent_operation_id=command.parent_operation_id,
            run_id=state.run_id,
            turn_id=command.turn_id,
            kind=command.kind,
            name=command.name,
            request_hash=command.request_hash,
            idempotency=command.idempotency,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def _apply_recovery(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: RecoveryDecisionCommand,
        timestamp: str,
    ) -> tuple[AgentState, OperationRecord | None]:
        operation = self._operation_in(connection, command.operation_id) if command.operation_id else None
        if operation is not None and operation.run_id != state.run_id:
            raise StateInvariantError("Recovery Operation 不属于当前 Run")
        diagnostic = {
            **state.diagnostics,
            "last_recovery_decision": AuditSanitizer.sanitize({
                "action": command.action,
                "actor": command.actor,
                "reason": command.reason,
                "risk_confirmed": command.risk_confirmed,
                "at": timestamp,
            }),
        }
        if command.action == "confirm_succeeded":
            if operation is None or operation.status is not OperationStatus.UNKNOWN:
                raise StateInvariantError("只能人工确认 unknown Operation 成功")
            observed = command.observed_result or ""
            operation = operation.model_copy(update={
                "status": OperationStatus.COMPLETED,
                "result": AuditSanitizer.sanitize(observed),
                "result_hash": hashlib.sha256(observed.encode("utf-8")).hexdigest(),
                "result_source": "human_confirmed",
                "reconcile_evidence": f"actor={command.actor}; reason={command.reason}",
                "completed_at": timestamp,
                "updated_at": timestamp,
            })
        elif command.action == "retry":
            if operation is None or operation.status not in {OperationStatus.UNKNOWN, OperationStatus.FAILED}:
                raise StateInvariantError("只能重试 unknown/failed Operation")
            if operation.idempotency is ToolIdempotency.NON_IDEMPOTENT and not command.risk_confirmed:
                raise StateInvariantError("NON_IDEMPOTENT Operation 重试必须人工确认风险")
            operation = operation.model_copy(update={
                "status": OperationStatus.PREPARED,
                "unknown_reason": None,
                "error": None,
                "heartbeat_at": None,
                "heartbeat_expires_at": None,
                "updated_at": timestamp,
            })
        elif command.action == "fail" and operation is not None:
            operation = operation.model_copy(update={
                "status": OperationStatus.FAILED,
                "error": AuditSanitizer.sanitize(command.reason),
                "completed_at": timestamp,
                "updated_at": timestamp,
            })
        elif command.action == "cancel":
            state = state.model_copy(update={"cancellation_requested": True})
        return state.model_copy(update={"diagnostics": diagnostic, "last_progress_at": timestamp}), operation

    def _write_event(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        event_type: str,
        payload: dict[str, Any],
    ) -> GatewayEventEnvelope:
        row = connection.execute(
            "SELECT last_sequence FROM event_sequences WHERE run_id=?", (state.run_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO event_sequences(run_id,last_sequence) VALUES(?,0)", (state.run_id,),
            )
            sequence = 1
        else:
            sequence = int(row["last_sequence"]) + 1
        connection.execute(
            "UPDATE event_sequences SET last_sequence=? WHERE run_id=?", (sequence, state.run_id),
        )
        event = GatewayEventEnvelope(
            event_id=uuid4().hex,
            sequence=sequence,
            timestamp=now_iso(),
            project_id=state.project_id,
            session_id=state.session_id,
            run_id=state.run_id,
            type=event_type,
            payload=AuditSanitizer.sanitize(payload),
        )
        connection.execute(
            "INSERT INTO gateway_events VALUES(?,?,?,?,?)",
            (event.event_id, event.run_id, event.sequence, event.model_dump_json(), event.timestamp),
        )
        connection.execute(
            "INSERT INTO event_outbox(event_id,run_id,sequence,created_at,updated_at) VALUES(?,?,?,?,?)",
            (event.event_id, event.run_id, event.sequence, event.timestamp, event.timestamp),
        )
        return event

    @staticmethod
    def _event_type(command: Command) -> str:
        names = {
            TransitionCommand: "state_transitioned",
            UpdateStateMetadataCommand: "state_metadata_updated",
            RecordRuntimeEventCommand: command.event_type if isinstance(command, RecordRuntimeEventCommand) else "runtime_event",
            BindSessionCommand: "session_bound",
            FinalizeInboxCommand: "inbox_created",
            BeginOperationCommand: "operation_prepared",
            StartOperationCommand: "operation_started",
            CompleteOperationCommand: "operation_completed",
            FailOperationCommand: "operation_failed",
            MarkOperationUnknownCommand: "operation_unknown",
            AbandonOperationCommand: "operation_abandoned",
            HeartbeatOperationCommand: "operation_heartbeat",
            ReconcileOperationCommand: "operation_reconciled",
            CreateApprovalCommand: "approval_created",
            DecideApprovalCommand: "approval_decided",
            ExpireApprovalCommand: "approval_expired",
            RequestCancellationCommand: "cancellation_requested",
            CreateSafeCheckpointCommand: "safe_checkpoint_created",
            RecoveryDecisionCommand: "recovery_decided",
            AdoptGatewayEpochCommand: "gateway_epoch_adopted",
        }
        return names[type(command)]

    @staticmethod
    def _event_payload(
        command: Command,
        before: AgentState,
        after: AgentState,
        operation: OperationRecord | None,
        approval: DurableApproval | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "command_id": command.command_id,
            "revision": after.revision,
            "task_state": after.task_state.value,
            "execution_state": after.execution.state.value if after.execution else None,
        }
        if isinstance(command, RecordRuntimeEventCommand):
            return {**AuditSanitizer.sanitize(command.payload), "revision": after.revision}
        if isinstance(command, TransitionCommand):
            payload.update({
                "from_task_state": before.task_state.value,
                "to_task_state": after.task_state.value,
                "from_execution_state": before.execution.state.value if before.execution else None,
                "to_execution_state": after.execution.state.value if after.execution else None,
                "reason": command.reason,
            })
        if operation is not None:
            payload.update({"operation_id": operation.operation_id, "operation_status": operation.status.value})
        if approval is not None:
            payload.update({"approval_id": approval.approval_id, "approval_status": approval.status.value})
        return payload

    @staticmethod
    def _state_row(state: AgentState) -> tuple[Any, ...]:
        return (
            state.run_id,
            state.revision,
            state.gateway_epoch,
            state.task_state.value,
            state.execution.state.value if state.execution else None,
            int(state.recovery_required),
            state.model_dump_json(),
            state.updated_at,
        )

    def _update_run_projection(self, connection: sqlite3.Connection, state: AgentState) -> None:
        # runs 是兼容投影；业务判断必须读取 agent_states。
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
        if not columns:
            return
        updates = ["status=?", "session_id=?", "started_at=?", "finished_at=?", "answer=?", "error=?"]
        values: list[Any] = [
            projected_run_status(state), state.session_id, state.started_at, state.finished_at,
            state.result_summary if state.task_state is TaskState.SUCCEEDED else None,
            state.error,
        ]
        optional = {
            "task_state": state.task_state.value,
            "execution_state": state.execution.state.value if state.execution else None,
            "execution_outcome": state.execution.outcome.value if state.execution and state.execution.outcome else None,
            "finish_reason": state.execution.finish_reason if state.execution else None,
            "state_revision": state.revision,
            "recovery_required": int(state.recovery_required),
            "terminal_target": state.terminal_target.value if state.terminal_target else None,
            "workload_kind": state.workload_kind.value,
        }
        for key, value in optional.items():
            if key in columns:
                updates.append(f"{key}=?")
                values.append(value)
        values.append(state.run_id)
        connection.execute(f"UPDATE runs SET {','.join(updates)} WHERE run_id=?", values)

    @staticmethod
    def _save_operation(connection: sqlite3.Connection, operation: OperationRecord) -> None:
        connection.execute(
            "INSERT INTO operation_ledger(operation_id,run_id,parent_operation_id,kind,status,record_json,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET status=excluded.status,"
            "record_json=excluded.record_json,updated_at=excluded.updated_at",
            (
                operation.operation_id, operation.run_id, operation.parent_operation_id,
                operation.kind.value, operation.status.value, operation.model_dump_json(), operation.updated_at,
            ),
        )

    @staticmethod
    def _save_approval(connection: sqlite3.Connection, approval: DurableApproval) -> None:
        connection.execute(
            "INSERT INTO durable_approvals(approval_id,run_id,operation_id,status,approval_json,expires_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status,"
            "approval_json=excluded.approval_json,updated_at=excluded.updated_at",
            (
                approval.approval_id, approval.run_id, approval.operation_id,
                approval.status.value, approval.model_dump_json(), approval.expires_at, approval.decided_at or approval.created_at,
            ),
        )

    @staticmethod
    def _state_in(connection: sqlite3.Connection, run_id: str) -> AgentState:
        row = connection.execute("SELECT state_json FROM agent_states WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知 AgentState：{run_id}")
        return AgentState.model_validate_json(row["state_json"], strict=True)

    @staticmethod
    def _operation_in(connection: sqlite3.Connection, operation_id: str) -> OperationRecord:
        row = connection.execute(
            "SELECT record_json FROM operation_ledger WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"未知 Operation：{operation_id}")
        return OperationRecord.model_validate_json(row["record_json"], strict=True)

    @staticmethod
    def _approval_in(connection: sqlite3.Connection, approval_id: str) -> DurableApproval:
        row = connection.execute(
            "SELECT approval_json FROM durable_approvals WHERE approval_id=?", (approval_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"未知 Approval：{approval_id}")
        return DurableApproval.model_validate_json(row["approval_json"], strict=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self):
        """提交或回滚事务后始终关闭 SQLite 句柄。"""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _restore_backup(self, backup_path: Path) -> None:
        with sqlite3.connect(backup_path, timeout=30) as source:
            with sqlite3.connect(self.database_path, timeout=30) as target:
                source.backup(target)


__all__ = ["StateConflictError", "StateController", "StateInvariantError"]


def _age_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, (now - datetime.fromisoformat(value)).total_seconds())
    except ValueError:
        return None
