"""Durable Runtime 的唯一状态修改入口。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

if TYPE_CHECKING:
    from backup import AgentHomeWriteGate

from Agent.state import (
    AbandonOperationAttemptCommand,
    AbandonOperationCommand,
    AdoptGatewayEpochCommand,
    AgentState,
    ApplyResult,
    ApprovalStatus,
    AttemptRecoveryResolution,
    BeginOperationAttemptCommand,
    BeginOperationCommand,
    BindSessionCommand,
    Command,
    CompleteOperationCommand,
    CompleteOperationAttemptCommand,
    CreateOperationWithAttemptCommand,
    CreateApprovalCommand,
    CreateSafeCheckpointCommand,
    DecideApprovalCommand,
    DurableApproval,
    ExecutionOutcome,
    ExecutionSnapshot,
    ExecutionState,
    ExpireApprovalCommand,
    FailOperationCommand,
    FailOperationAttemptCommand,
    FinalizeAuditCommand,
    FinalizeInboxCommand,
    FinalizeTerminalCommand,
    FinalizeGenerationRecord,
    HeartbeatOperationCommand,
    HeartbeatOperationAttemptCommand,
    MarkOperationUnknownCommand,
    MarkOperationAttemptUnknownCommand,
    OperationKind,
    OperationAttempt,
    OperationFailureKind,
    OperationRecord,
    OperationStatus,
    PersistenceContract,
    RecordRuntimeEventCommand,
    ReconcileOperationCommand,
    ReconcileOperationAttemptCommand,
    ReconcileStatus,
    RecoveryDecisionCommand,
    RequestCancellationCommand,
    RetryPolicySnapshot,
    StartOperationCommand,
    StartOperationAttemptCommand,
    StartFinalizeGenerationCommand,
    StartReplacementFinalizeGenerationCommand,
    InvalidateFinalizeGenerationCommand,
    SkipOperationAttemptCommand,
    TaskState,
    TERMINAL_STATES,
    TerminalTarget,
    ToolIdempotency,
    TransitionCommand,
    UpdateStateMetadataCommand,
    UpgradePersistenceContractCommand,
    WorkloadKind,
    reduce_operation,
    projected_run_status,
    validate_inner_transition,
    validate_outer_transition,
)
from gateway.audit import AuditSanitizer
from gateway.finalize_evidence import (
    FINALIZE_PROTOCOL_VERSION,
    FinalizeEvidenceCodec,
    FinalizeIdentity,
    FinalizeRequirementPolicy,
    FinalizeStep,
    NotApplicableEvidence,
    VerifiedArtifactEvidence,
)
from gateway.models import GatewayEventEnvelope, now_iso


class StateConflictError(RuntimeError):
    """revision、fencing 或命令内容冲突。"""


class StateInvariantError(RuntimeError):
    """命令违反 FSM、Operation 或 Checkpoint 不变量。"""


class StateController:
    """以 SQLite 事务实现 command 幂等、CAS、FSM guard 和 Outbox。"""

    SCHEMA_VERSION = 7

    def __init__(
        self,
        database_path: Path,
        *,
        gateway_epoch: str,
        migration_backup_path: Path | None = None,
        write_gate: "AgentHomeWriteGate | None" = None,
    ) -> None:
        self.database_path = database_path.resolve()
        self.gateway_epoch = gateway_epoch
        self.migration_backup_path = migration_backup_path
        self.write_gate = write_gate
        self._backup_before_state_migration()
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
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS operation_ledger (
                    operation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    parent_operation_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
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
                    updated_at TEXT NOT NULL,
                    attempt_id TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(operation_id) REFERENCES operation_ledger(operation_id),
                    FOREIGN KEY(attempt_id) REFERENCES operation_attempts(attempt_id)
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
                    client_id TEXT NOT NULL,
                    operation_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(client_id,operation_name,idempotency_key)
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
                CREATE TABLE IF NOT EXISTS operation_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
                    request_hash TEXT NOT NULL,
                    side_effecting INTEGER NOT NULL CHECK(side_effecting IN (0,1)),
                    status TEXT NOT NULL CHECK(status IN
                        ('prepared','running','completed','failed','skipped','unknown','abandoned')),
                    failure_kind TEXT,
                    recovery_resolution TEXT,
                    attempt_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operation_ledger(operation_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    UNIQUE(operation_id, attempt_no),
                    CHECK(
                        (status='failed' AND failure_kind IN ('terminal','retryable')) OR
                        (status='unknown' AND failure_kind='unknown_effect'
                            AND recovery_resolution IN
                                ('unresolved','retry_authorized','confirmed_succeeded','confirmed_failed')) OR
                        (status NOT IN ('failed','unknown') AND failure_kind IS NULL)
                    )
                );
                CREATE TABLE IF NOT EXISTS recovery_decisions (
                    decision_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    operation_id TEXT,
                    attempt_id TEXT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(operation_id) REFERENCES operation_ledger(operation_id),
                    FOREIGN KEY(attempt_id) REFERENCES operation_attempts(attempt_id)
                );
                CREATE TABLE IF NOT EXISTS finalize_generations (
                    run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    protocol_version INTEGER NOT NULL CHECK(protocol_version = 2),
                    generation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,generation),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS finalize_generation_invalidations (
                    invalidation_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id,generation) REFERENCES finalize_generations(run_id,generation)
                );
                CREATE TABLE IF NOT EXISTS run_audit_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    finalize_generation INTEGER NOT NULL,
                    receipt_generation INTEGER NOT NULL CHECK(receipt_generation >= 1),
                    receipt_json TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id,receipt_generation),
                    FOREIGN KEY(run_id,finalize_generation)
                        REFERENCES finalize_generations(run_id,generation)
                );
                CREATE TABLE IF NOT EXISTS run_current_audit_receipt (
                    run_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(receipt_id) REFERENCES run_audit_receipts(receipt_id)
                );
                CREATE TABLE IF NOT EXISTS harness_dream_changesets (
                    stable_key TEXT PRIMARY KEY,
                    source_identity TEXT NOT NULL,
                    dream_date TEXT NOT NULL,
                    automatic_cycle INTEGER NOT NULL CHECK(automatic_cycle IN (0,1)),
                    cutoff_at TEXT NOT NULL,
                    changeset_hash TEXT NOT NULL,
                    changeset_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('discovered','running','success','no_changes','deferred',
                         'blocked','failed','unknown','restart_wait_timeout')),
                    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
                    active_run_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
                    cursor_occurred_at TEXT,
                    cursor_event_id TEXT,
                    cursor_committed_at TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_identity,dream_date,automatic_cycle)
                );
                CREATE TABLE IF NOT EXISTS harness_dream_generations (
                    stable_key TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY(stable_key,generation),
                    FOREIGN KEY(stable_key) REFERENCES harness_dream_changesets(stable_key)
                );
                CREATE TABLE IF NOT EXISTS harness_dream_freezes (
                    source_identity TEXT NOT NULL,
                    dream_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source_identity,dream_date)
                );
                CREATE TABLE IF NOT EXISTS harness_dream_reverts (
                    proposal_id TEXT PRIMARY KEY,
                    stable_key TEXT NOT NULL,
                    operation_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(stable_key) REFERENCES harness_dream_changesets(stable_key)
                );
                CREATE TABLE IF NOT EXISTS gateway_restart_requests (
                    request_id TEXT PRIMARY KEY,
                    stable_key TEXT NOT NULL UNIQUE,
                    expected_pid INTEGER NOT NULL,
                    expected_gateway_epoch TEXT NOT NULL,
                    expected_commit TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(stable_key) REFERENCES harness_dream_changesets(stable_key)
                );
                """
            )
            self._ensure_column(connection, "operation_ledger", "stable_key", "TEXT")
            self._ensure_column(connection, "operation_ledger", "request_hash", "TEXT")
            self._ensure_column(connection, "operation_ledger", "side_effecting", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "durable_approvals", "attempt_id", "TEXT")
            self._ensure_column(connection, "event_outbox", "eventbus_next_attempt_at", "TEXT")
            self._ensure_column(connection, "event_outbox", "jsonl_next_attempt_at", "TEXT")
            self._ensure_column(connection, "event_outbox", "eventbus_dead_letter_at", "TEXT")
            self._ensure_column(connection, "event_outbox", "jsonl_dead_letter_at", "TEXT")
            self._migrate_idempotency_scope(connection)
            self._migrate_legacy_operations(connection)
            self._migrate_legacy_approvals(connection)
            connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS operation_stable_key_uq
                    ON operation_ledger(run_id,kind,stable_key);
                CREATE INDEX IF NOT EXISTS operation_attempts_run_idx
                    ON operation_attempts(run_id,status);
                CREATE UNIQUE INDEX IF NOT EXISTS operation_attempt_model_call_uq
                    ON operation_attempts(json_extract(attempt_json,'$.model_call_id'))
                    WHERE json_extract(attempt_json,'$.model_call_id') IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS operation_attempt_external_request_uq
                    ON operation_attempts(json_extract(attempt_json,'$.external_request_id'))
                    WHERE json_extract(attempt_json,'$.external_request_id') IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_side_effect_attempt_per_run
                    ON operation_attempts(run_id)
                    WHERE side_effecting=1 AND (
                        status IN ('prepared','running') OR
                        (status='unknown' AND recovery_resolution='unresolved')
                    );
                DROP INDEX IF EXISTS one_pending_approval_per_attempt;
                CREATE UNIQUE INDEX IF NOT EXISTS one_approval_per_attempt
                    ON durable_approvals(attempt_id) WHERE attempt_id IS NOT NULL;
                DROP TRIGGER IF EXISTS normal_terminal_requires_finalizing;
                CREATE TRIGGER normal_terminal_requires_finalizing
                BEFORE UPDATE OF task_state ON agent_states
                WHEN NEW.task_state IN ('succeeded','failed','cancelled')
                     AND NEW.task_state!=OLD.task_state
                     AND OLD.task_state!='finalizing'
                BEGIN
                    SELECT RAISE(ABORT, 'normal terminal state requires FINALIZING');
                END;
                DROP TRIGGER IF EXISTS immutable_run_audit_receipt_update;
                CREATE TRIGGER immutable_run_audit_receipt_update
                BEFORE UPDATE ON run_audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'run audit receipt is immutable');
                END;
                DROP TRIGGER IF EXISTS immutable_run_audit_receipt_delete;
                CREATE TRIGGER immutable_run_audit_receipt_delete
                BEFORE DELETE ON run_audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'run audit receipt is immutable');
                END;
                DROP TRIGGER IF EXISTS immutable_finalize_generation_update;
                CREATE TRIGGER immutable_finalize_generation_update
                BEFORE UPDATE ON finalize_generations
                BEGIN
                    SELECT RAISE(ABORT, 'finalize generation is immutable');
                END;
                DROP TRIGGER IF EXISTS immutable_finalize_generation_delete;
                CREATE TRIGGER immutable_finalize_generation_delete
                BEFORE DELETE ON finalize_generations
                BEGIN
                    SELECT RAISE(ABORT, 'finalize generation is immutable');
                END;
                DROP TRIGGER IF EXISTS finished_attempt_is_immutable;
                CREATE TRIGGER finished_attempt_is_immutable
                BEFORE UPDATE OF status ON operation_attempts
                WHEN OLD.status IN ('completed','failed','skipped','abandoned')
                     AND NEW.status!=OLD.status
                BEGIN
                    SELECT RAISE(ABORT, 'finished OperationAttempt is immutable');
                END;
                """
            )
            connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(f"Gateway SQLite foreign_key_check 失败：{foreign_key_errors[:5]}")
            final_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if final_check != "ok":
                raise RuntimeError(f"Gateway SQLite migration 后 quick_check 失败：{final_check}")

    def _backup_before_state_migration(self) -> None:
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return
        with sqlite3.connect(self.database_path, timeout=30) as source:
            current = int(source.execute("PRAGMA user_version").fetchone()[0])
            has_state = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_states'",
            ).fetchone() is not None
            if current >= self.SCHEMA_VERSION or not has_state:
                return
            if self.migration_backup_path is None:
                directory = self.database_path.parent / "backups"
                directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
                self.migration_backup_path = (
                    directory / f"gateway-state-v{current}-to-v{self.SCHEMA_VERSION}-{stamp}.sqlite3"
                )
            with sqlite3.connect(self.migration_backup_path, timeout=30) as target:
                source.backup(target)

    def create_state(
        self,
        *,
        run_id: str,
        workload_kind: WorkloadKind,
        project_id: str,
        client_id: str,
        idempotency_key: str,
        request_hash: str,
        persistence_contract: PersistenceContract | None = None,
        session_id: str | None = None,
        parent_run_id: str | None = None,
        deadline_at: str | None = None,
    ) -> AgentState:
        timestamp = now_iso()
        state = AgentState(
            gateway_epoch=self.gateway_epoch,
            run_id=run_id,
            workload_kind=workload_kind,
            persistence_contract=persistence_contract or _default_persistence_contract(workload_kind),
            project_id=project_id,
            session_id=session_id,
            client_id=client_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            turn_id=f"turn:{run_id}",
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
        persistence_contract: PersistenceContract | None = None,
        session_id: str | None = None,
        parent_run_id: str | None = None,
        deadline_at: str | None = None,
    ) -> tuple[AgentState, bool]:
        """原子建立 Run 投影、幂等记录、AgentState、Event 与 Outbox。"""
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_hash,run_id FROM idempotency_records "
                "WHERE client_id=? AND operation_name=? AND idempotency_key=?",
                (client_id, workload_kind.value, idempotency_key),
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
                persistence_contract=persistence_contract or _default_persistence_contract(workload_kind),
                project_id=project_id,
                session_id=session_id,
                client_id=client_id,
                parent_run_id=parent_run_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                turn_id=f"turn:{run_id}",
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
                "INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?,?)",
                (client_id, workload_kind.value, idempotency_key, request_hash,
                 run_id, None, timestamp, timestamp),
            )
            self._update_run_projection(connection, state)
            self._write_event(connection, state, "state_created", {"task_state": state.task_state.value})
            connection.commit()
            return state, False

    def apply(self, command: Command) -> ApplyResult:
        if self.write_gate is not None:
            self.write_gate.check_mutation_admission()
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
            attempt: OperationAttempt | None = None
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
                if command.task_state in {
                    TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
                }:
                    raise StateInvariantError(
                        "正常终态只能由 FinalizeTerminalCommand 从 FINALIZING 提交",
                    )
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
            elif isinstance(command, UpgradePersistenceContractCommand):
                if state.task_state in {TaskState.FINALIZING} | set(TERMINAL_STATES):
                    raise StateInvariantError("Persistence contract must be fixed before FINALIZING")
                if state.persistence_contract is not PersistenceContract.CONTROL_ONLY:
                    if state.persistence_contract is not command.persistence_contract:
                        raise StateConflictError("Persistence contract cannot be replaced or downgraded")
                else:
                    state = state.model_copy(update={
                        "persistence_contract": command.persistence_contract,
                    })
            elif isinstance(command, StartReplacementFinalizeGenerationCommand):
                if state.task_state is not TaskState.FINALIZING or state.terminal_target is None:
                    raise StateInvariantError("Replacement finalize generation requires FINALIZING")
                if state.finalize_generation != command.invalidated_generation:
                    raise StateConflictError("Finalize generation changed before replacement")
                if command.generation != command.invalidated_generation + 1:
                    raise StateInvariantError("Finalize generation must increase by exactly one")
                invalidated = connection.execute(
                    "SELECT 1 FROM finalize_generation_invalidations WHERE run_id=? AND generation=?",
                    (state.run_id, command.invalidated_generation),
                ).fetchone()
                if invalidated is None:
                    raise StateInvariantError("Replacement requires a durable invalidation")
                record = FinalizeGenerationRecord(
                    run_id=state.run_id,
                    generation=command.generation,
                    terminal_target=state.terminal_target,
                    persistence_contract=state.persistence_contract,
                    supersedes_generation=command.invalidated_generation,
                    created_at=timestamp,
                )
                self._insert_finalize_generation(connection, record)
                state = state.model_copy(update={"finalize_generation": command.generation})
            elif isinstance(command, StartFinalizeGenerationCommand):
                if state.task_state is not TaskState.FINALIZING or state.terminal_target is None:
                    raise StateInvariantError("Finalize generation requires FINALIZING")
                if state.finalize_generation is None:
                    if command.generation != 1 or command.supersedes_generation is not None:
                        raise StateInvariantError("First finalize generation must be generation 1")
                    record = FinalizeGenerationRecord(
                        run_id=state.run_id,
                        generation=1,
                        terminal_target=state.terminal_target,
                        persistence_contract=state.persistence_contract,
                        created_at=timestamp,
                    )
                    self._insert_finalize_generation(connection, record)
                    state = state.model_copy(update={"finalize_generation": 1})
                elif state.finalize_generation != command.generation:
                    raise StateConflictError("Finalize generation already exists")
            elif isinstance(command, InvalidateFinalizeGenerationCommand):
                if state.finalize_generation != command.generation:
                    raise StateConflictError("Only the current finalize generation can be invalidated")
                connection.execute(
                    "INSERT INTO finalize_generation_invalidations"
                    "(invalidation_id,command_id,run_id,generation,reason,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        uuid4().hex, command.command_id, state.run_id, command.generation,
                        AuditSanitizer.sanitize(command.reason), timestamp,
                    ),
                )
            elif isinstance(command, FinalizeAuditCommand):
                operation, attempt = self._finalize_audit(connection, state, command, timestamp)
            elif isinstance(command, FinalizeInboxCommand):
                operation, attempt = self._finalize_inbox(connection, state, command, timestamp)
            elif isinstance(command, FinalizeTerminalCommand):
                if state.task_state is not TaskState.FINALIZING or state.terminal_target is None:
                    raise StateInvariantError("FinalizeTerminalCommand 需要 FINALIZING + terminal_target")
                self._validate_terminal_finalize(connection, state, command.generation)
                state = self._apply_transition(
                    state,
                    TransitionCommand(
                        command_id=command.command_id, run_id=command.run_id,
                        expected_revision=command.expected_revision,
                        gateway_epoch=command.gateway_epoch, reason=command.reason,
                        task_state=TaskState(state.terminal_target.value),
                    ),
                    timestamp,
                )
                transition = True
            elif isinstance(command, CreateOperationWithAttemptCommand):
                operation, attempt = self._create_operation_with_attempt(connection, state, command, timestamp)
                state = state.model_copy(update={
                    "current_operation_id": operation.operation_id,
                    "current_attempt_id": attempt.attempt_id,
                    "turn_id": command.turn_id or state.turn_id,
                    **({"model_call_id": attempt.model_call_id} if command.kind is OperationKind.MODEL else {}),
                    **({"tool_call_id": command.tool_call_id or operation.operation_id}
                       if command.kind is OperationKind.TOOL else {}),
                })
            elif isinstance(command, BeginOperationAttemptCommand):
                operation, attempt = self._begin_operation_attempt(connection, state, command, timestamp)
                state = state.model_copy(update={
                    "current_operation_id": operation.operation_id,
                    "current_attempt_id": attempt.attempt_id,
                    **({"model_call_id": attempt.model_call_id} if operation.kind is OperationKind.MODEL else {}),
                })
            elif isinstance(command, StartOperationAttemptCommand):
                operation, attempt = self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED},
                    updates={"status": OperationStatus.RUNNING, "started_at": timestamp,
                             "heartbeat_at": timestamp, "heartbeat_expires_at": command.heartbeat_expires_at},
                )
                state = state.model_copy(update={
                    "current_operation_id": operation.operation_id, "current_attempt_id": attempt.attempt_id,
                    "operation_started_at": timestamp, "operation_heartbeat_at": timestamp,
                    "last_progress_at": timestamp,
                })
            elif isinstance(command, CompleteOperationAttemptCommand):
                selected_attempt = self._attempt_in(connection, command.attempt_id)
                selected_operation = self._operation_in(connection, selected_attempt.operation_id)
                if selected_operation.stable_key.startswith("finalize:v2:"):
                    if selected_attempt.status is not OperationStatus.RUNNING:
                        raise StateInvariantError(
                            "Finalize file Attempt must be RUNNING before completion",
                        )
                    self._validate_finalize_evidence(
                        state, selected_operation, selected_attempt,
                        command.result, command.result_hash,
                    )
                operation, attempt = self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.RUNNING, OperationStatus.UNKNOWN},
                    updates={"status": OperationStatus.COMPLETED, "failure_kind": None,
                             "failure_reason": None, "recovery_resolution": None,
                             "result": AuditSanitizer.sanitize(command.result),
                             "result_hash": command.result_hash, "result_source": command.result_source,
                             "completed_at": timestamp},
                )
                state = state.model_copy(update={"last_progress_at": timestamp})
            elif isinstance(command, FailOperationAttemptCommand):
                operation, attempt = self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED, OperationStatus.RUNNING},
                    updates={"status": OperationStatus.FAILED, "failure_kind": command.failure_kind,
                             "failure_reason": AuditSanitizer.sanitize(command.failure_reason),
                             "completed_at": timestamp},
                )
                state = state.model_copy(update={"last_progress_at": timestamp})
            elif isinstance(command, MarkOperationAttemptUnknownCommand):
                operation, attempt = self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.RUNNING},
                    updates={"status": OperationStatus.UNKNOWN,
                             "failure_kind": OperationFailureKind.UNKNOWN_EFFECT,
                             "failure_reason": AuditSanitizer.sanitize(command.failure_reason),
                             "recovery_resolution": AttemptRecoveryResolution.UNRESOLVED},
                )
                state = state.model_copy(update={"recovery_reason": attempt.failure_reason})
            elif isinstance(command, SkipOperationAttemptCommand):
                operation, attempt = self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED},
                    updates={"status": OperationStatus.SKIPPED, "result_source": "NOT_EXECUTED",
                             "skip_reason": AuditSanitizer.sanitize(command.skip_reason),
                             "completed_at": timestamp},
                )
            elif isinstance(command, AbandonOperationAttemptCommand):
                operation, attempt = self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED, OperationStatus.RUNNING},
                    updates={"status": OperationStatus.ABANDONED,
                             "abandonment_reason": AuditSanitizer.sanitize(command.abandonment_reason),
                             "completed_at": timestamp},
                )
            elif isinstance(command, HeartbeatOperationAttemptCommand):
                operation, attempt = self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.RUNNING},
                    updates={"heartbeat_at": command.heartbeat_at,
                             "heartbeat_expires_at": command.heartbeat_expires_at},
                )
                state = state.model_copy(update={"operation_heartbeat_at": command.heartbeat_at})
            elif isinstance(command, ReconcileOperationAttemptCommand):
                operation, attempt = self._reconcile_attempt(connection, state, command, timestamp)
            elif isinstance(command, BeginOperationCommand):
                operation = self._begin_operation(connection, state, command, timestamp)
                attempt = self._current_attempt_in(connection, operation.operation_id)
                state = state.model_copy(update={
                    "current_operation_id": operation.operation_id,
                    "current_attempt_id": attempt.attempt_id,
                    "turn_id": command.turn_id or state.turn_id,
                    **({"model_call_id": operation.operation_id} if command.kind is OperationKind.MODEL else {}),
                    **({"tool_call_id": operation.operation_id} if command.kind is OperationKind.TOOL else {}),
                })
            elif isinstance(command, StartOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                selected = self._current_attempt_in(connection, operation.operation_id)
                operation, attempt = self._update_attempt(
                    connection, state, selected.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED},
                    updates={"status": OperationStatus.RUNNING, "started_at": timestamp,
                             "heartbeat_at": timestamp,
                             "heartbeat_expires_at": command.heartbeat_expires_at},
                )
                state = state.model_copy(update={
                    "operation_started_at": timestamp,
                    "operation_heartbeat_at": timestamp,
                    "last_progress_at": timestamp,
                })
            elif isinstance(command, CompleteOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                selected = self._current_attempt_in(connection, operation.operation_id)
                operation, attempt = self._update_attempt(
                    connection, state, selected.attempt_id, timestamp,
                    allowed={OperationStatus.RUNNING, OperationStatus.UNKNOWN},
                    updates={"status": OperationStatus.COMPLETED, "failure_kind": None,
                             "failure_reason": None, "recovery_resolution": None,
                             "result": AuditSanitizer.sanitize(command.result),
                             "result_hash": command.result_hash,
                             "result_source": command.result_source, "completed_at": timestamp},
                )
                state = state.model_copy(update={"last_progress_at": timestamp})
            elif isinstance(command, FailOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                selected = self._current_attempt_in(connection, operation.operation_id)
                operation, attempt = self._update_attempt(
                    connection, state, selected.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED, OperationStatus.RUNNING},
                    updates={"status": OperationStatus.FAILED,
                             "failure_kind": command.failure_kind,
                             "failure_reason": AuditSanitizer.sanitize(command.error),
                             "completed_at": timestamp},
                )
                state = state.model_copy(update={"last_progress_at": timestamp})
            elif isinstance(command, MarkOperationUnknownCommand):
                operation = self._operation_in(connection, command.operation_id)
                selected = self._current_attempt_in(connection, operation.operation_id)
                operation, attempt = self._update_attempt(
                    connection, state, selected.attempt_id, timestamp,
                    allowed={OperationStatus.RUNNING},
                    updates={"status": OperationStatus.UNKNOWN,
                             "failure_kind": OperationFailureKind.UNKNOWN_EFFECT,
                             "failure_reason": AuditSanitizer.sanitize(command.unknown_reason),
                             "recovery_resolution": AttemptRecoveryResolution.UNRESOLVED},
                )
                state = state.model_copy(update={"recovery_reason": operation.unknown_reason})
            elif isinstance(command, AbandonOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                if operation.run_id != state.run_id or operation.kind is not OperationKind.MODEL:
                    raise StateInvariantError("只有当前 Run 的模型 Operation 可以 abandoned")
                selected = self._current_attempt_in(connection, operation.operation_id)
                operation, attempt = self._update_attempt(
                    connection, state, selected.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED, OperationStatus.RUNNING},
                    updates={"status": OperationStatus.ABANDONED,
                             "abandonment_reason": AuditSanitizer.sanitize(command.reason),
                             "completed_at": timestamp},
                )
            elif isinstance(command, HeartbeatOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                selected = self._current_attempt_in(connection, operation.operation_id)
                operation, attempt = self._update_attempt(
                    connection, state, selected.attempt_id, timestamp,
                    allowed={OperationStatus.RUNNING},
                    updates={"heartbeat_at": command.heartbeat_at,
                             "heartbeat_expires_at": command.heartbeat_expires_at},
                )
                state = state.model_copy(update={"operation_heartbeat_at": command.heartbeat_at})
            elif isinstance(command, ReconcileOperationCommand):
                operation = self._operation_in(connection, command.operation_id)
                selected = self._current_attempt_in(connection, operation.operation_id)
                operation, attempt = self._reconcile_attempt(
                    connection, state,
                    ReconcileOperationAttemptCommand(
                        command_id=command.command_id, run_id=command.run_id,
                        expected_revision=command.expected_revision,
                        gateway_epoch=command.gateway_epoch,
                        attempt_id=selected.attempt_id, result=command.result,
                    ),
                    timestamp,
                )
            elif isinstance(command, CreateApprovalCommand):
                approval = command.approval
                if approval.run_id != state.run_id:
                    raise StateInvariantError("Approval 不属于当前 Run")
                selected_attempt = self._attempt_in(connection, approval.attempt_id)
                selected_operation = self._operation_in(connection, approval.operation_id)
                if (
                    selected_attempt.operation_id != approval.operation_id
                    or selected_attempt.attempt_no != approval.attempt_no
                    or selected_operation.request_hash != approval.request_hash
                ):
                    raise StateInvariantError("Approval 与已冻结 Tool Attempt 请求不一致")
                connection.execute(
                    "INSERT INTO durable_approvals(approval_id,run_id,operation_id,status,approval_json,expires_at,updated_at,attempt_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        approval.approval_id, approval.run_id, approval.operation_id,
                        approval.status.value, approval.model_dump_json(), approval.expires_at, timestamp,
                        approval.attempt_id,
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
                    if desired is ApprovalStatus.DENIED and approval.attempt_id:
                        operation, attempt = self._update_attempt(
                            connection, state, approval.attempt_id, timestamp,
                            allowed={OperationStatus.PREPARED},
                            updates={"status": OperationStatus.SKIPPED,
                                     "result_source": "NOT_EXECUTED",
                                     "skip_reason": "approval_denied",
                                     "completed_at": timestamp},
                        )
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
                    if approval.attempt_id:
                        operation, attempt = self._update_attempt(
                            connection, state, approval.attempt_id, timestamp,
                            allowed={OperationStatus.PREPARED},
                            updates={"status": OperationStatus.SKIPPED,
                                     "result_source": "NOT_EXECUTED",
                                     "skip_reason": "approval_timeout",
                                     "completed_at": timestamp},
                        )
            elif isinstance(command, RequestCancellationCommand):
                state = state.model_copy(update={
                    "cancellation_requested": True,
                    "recovery_reason": AuditSanitizer.sanitize(command.reason),
                })
            elif isinstance(command, CreateSafeCheckpointCommand):
                active = connection.execute(
                    "SELECT 1 FROM operation_attempts WHERE run_id=? AND side_effecting=1 AND ("
                    "status IN ('prepared','running') OR "
                    "(status='unknown' AND recovery_resolution='unresolved')) LIMIT 1",
                    (state.run_id,),
                ).fetchone()
                if active is not None:
                    raise StateInvariantError("存在结果未确定的副作用，不能创建 SafeCheckpoint")
                if command.checkpoint.run_id != state.run_id or command.checkpoint.state_revision != state.revision:
                    raise StateInvariantError("SafeCheckpoint 的 Run 或 revision 不匹配")
                if command.checkpoint.current_attempt_id:
                    selected_attempt = self._attempt_in(connection, command.checkpoint.current_attempt_id)
                    if selected_attempt.operation_id != command.checkpoint.current_operation_id:
                        raise StateInvariantError("SafeCheckpoint Operation/Attempt 引用不一致")
                if command.checkpoint.last_determined_attempt_id:
                    determined = self._attempt_in(connection, command.checkpoint.last_determined_attempt_id)
                    if determined.status in {
                        OperationStatus.PREPARED, OperationStatus.RUNNING, OperationStatus.UNKNOWN,
                    }:
                        raise StateInvariantError("SafeCheckpoint 不能指向未确定 Attempt")
                state = state.model_copy(update={"safe_checkpoint": command.checkpoint})
            elif isinstance(command, RecoveryDecisionCommand):
                state, operation, attempt = self._apply_recovery(
                    connection, state, command, timestamp,
                )
            else:  # pragma: no cover - Union 新增类型时强制实现
                raise TypeError(f"未实现的 State command：{type(command).__name__}")

            state = AgentState.model_validate(
                state.model_copy(update={"revision": state.revision + 1, "updated_at": timestamp}),
                strict=True,
            )
            if operation is not None:
                self._save_operation(connection, operation)
            if attempt is not None:
                self._save_attempt(connection, attempt)
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
                self._event_payload(command, previous, state, operation, attempt, approval),
            )
            result = ApplyResult(
                state=state, operation=operation, attempt=attempt,
                approval=approval, event_id=event.event_id,
            )
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

    def operation_attempts(self, operation_id: str) -> tuple[OperationAttempt, ...]:
        with self._connection() as connection:
            return tuple(self._attempts_in(connection, operation_id))

    def current_attempt(self, operation_id: str) -> OperationAttempt:
        with self._connection() as connection:
            return self._current_attempt_in(connection, operation_id)

    def current(self, run_id: str) -> AgentState:
        return self.state(run_id)

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

    def reserve_idempotency(
        self, key: str, request_hash: str, run_id: str,
        *, client_id: str = "legacy", operation_name: str = "run",
    ) -> str:
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_hash,run_id FROM idempotency_records "
                "WHERE client_id=? AND operation_name=? AND idempotency_key=?",
                (client_id, operation_name, key),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise StateConflictError("Idempotency-Key 已用于不同请求")
                connection.commit()
                return str(row["run_id"])
            connection.execute(
                "INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?,?)",
                (client_id, operation_name, key, request_hash, run_id, None, timestamp, timestamp),
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

    def claim_harness_dream(
        self,
        changeset: dict[str, Any],
        *,
        automatic_cycle: bool,
        no_changes: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Claim one immutable daily changeset before any Harness Run is created."""
        if self.write_gate is not None:
            self.write_gate.check_mutation_admission()
        stable_key = str(changeset["stable_key"])
        source_identity = str(changeset["source_identity"])
        dream_date = str(changeset["date"])
        timestamp = now_iso()
        canonical = json.dumps(
            AuditSanitizer.sanitize(changeset), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE "
                "(source_identity=? AND dream_date=? AND automatic_cycle=?) OR stable_key=? "
                "ORDER BY automatic_cycle DESC LIMIT 1",
                (source_identity, dream_date, int(automatic_cycle), stable_key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return dict(existing), True
            status = "no_changes" if no_changes else "discovered"
            last_event = changeset.get("last_event") or {}
            cursor_committed_at = timestamp if no_changes else None
            connection.execute(
                "INSERT INTO harness_dream_changesets("
                "stable_key,source_identity,dream_date,automatic_cycle,cutoff_at,changeset_hash,"
                "changeset_json,status,generation,active_run_id,revision,cursor_occurred_at,"
                "cursor_event_id,cursor_committed_at,result_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,NULL,0,?,?,?,?,?,?)",
                (
                    stable_key, source_identity, dream_date, int(automatic_cycle),
                    str(changeset["cutoff_at"]), str(changeset["changeset_hash"]), canonical,
                    status,
                    str(last_event.get("occurred_at") or "") or None,
                    str(last_event.get("merge_event_id") or "") or None,
                    cursor_committed_at,
                    json.dumps({"status": status}, ensure_ascii=False, sort_keys=True),
                    timestamp, timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            connection.commit()
            return dict(row), False

    def start_harness_dream_generation(
        self,
        stable_key: str,
        *,
        run_id: str,
        expected_revision: int,
        allow_blocked: bool = False,
    ) -> dict[str, Any]:
        """CAS the global changeset lock and allocate a new immutable execution generation."""
        if self.write_gate is not None:
            self.write_gate.check_mutation_admission()
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            if row is None:
                raise KeyError(stable_key)
            if int(row["revision"]) != expected_revision:
                raise StateConflictError("Harness Dream changeset revision conflict")
            if row["active_run_id"]:
                raise StateConflictError("Harness Dream changeset already has an active generation")
            allowed = {"discovered", "deferred", "failed", "restart_wait_timeout"}
            if allow_blocked:
                allowed.add("blocked")
            if str(row["status"]) not in allowed:
                raise StateConflictError(f"Harness Dream changeset cannot run from {row['status']}")
            generation = int(row["generation"]) + 1
            changed = connection.execute(
                "UPDATE harness_dream_changesets SET status='running',generation=?,active_run_id=?,"
                "revision=revision+1,updated_at=? WHERE stable_key=? AND revision=? AND active_run_id IS NULL",
                (generation, run_id, timestamp, stable_key, expected_revision),
            )
            if changed.rowcount != 1:
                raise StateConflictError("Harness Dream changeset CAS failed")
            connection.execute(
                "INSERT INTO harness_dream_generations VALUES(?,?,?,'running',NULL,?,NULL)",
                (stable_key, generation, run_id, timestamp),
            )
            selected = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            connection.commit()
            return dict(selected)

    def finish_harness_dream_generation(
        self,
        stable_key: str,
        *,
        run_id: str,
        expected_revision: int,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit outcome, scan cursor, Gateway Event and Outbox in one transaction."""
        allowed = {"success", "deferred", "blocked", "failed", "unknown"}
        if status not in allowed:
            raise ValueError(f"Unsupported Harness Dream outcome: {status}")
        timestamp = now_iso()
        result_json = json.dumps(
            AuditSanitizer.sanitize(result), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            if row is None:
                raise KeyError(stable_key)
            if int(row["revision"]) != expected_revision or str(row["active_run_id"]) != run_id:
                raise StateConflictError("Harness Dream completion CAS failed")
            state = self._state_in(connection, run_id)
            changeset = json.loads(str(row["changeset_json"]))
            last_event = changeset.get("last_event") or {}
            terminal_disposition = status != "unknown"
            connection.execute(
                "UPDATE harness_dream_changesets SET status=?,active_run_id=?,revision=revision+1,"
                "cursor_occurred_at=?,cursor_event_id=?,cursor_committed_at=?,result_json=?,updated_at=? "
                "WHERE stable_key=? AND revision=?",
                (
                    status, None if terminal_disposition else run_id,
                    str(last_event.get("occurred_at") or "") or None,
                    str(last_event.get("merge_event_id") or "") or None,
                    timestamp if terminal_disposition else None,
                    result_json, timestamp, stable_key, expected_revision,
                ),
            )
            connection.execute(
                "UPDATE harness_dream_generations SET status=?,result_json=?,completed_at=? "
                "WHERE stable_key=? AND generation=? AND run_id=?",
                (status, result_json, timestamp, stable_key, int(row["generation"]), run_id),
            )
            self._write_event(
                connection, state, f"harness_dream_{status}",
                {"stable_key": stable_key, "generation": int(row["generation"]), **result},
            )
            selected = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            connection.commit()
            return dict(selected)

    def harness_dream_changeset(
        self, *, stable_key: str | None = None, dream_date: str | None = None,
        source_identity: str | None = None,
    ) -> dict[str, Any] | None:
        if stable_key is None and dream_date is None:
            raise ValueError("stable_key or dream_date is required")
        with self._connection() as connection:
            if stable_key is not None:
                row = connection.execute(
                    "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM harness_dream_changesets WHERE dream_date=? "
                    "AND (? IS NULL OR source_identity=?) ORDER BY automatic_cycle DESC LIMIT 1",
                    (dream_date, source_identity, source_identity),
                ).fetchone()
        return dict(row) if row is not None else None

    def harness_dream_generations(self, stable_key: str) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM harness_dream_generations WHERE stable_key=? ORDER BY generation",
                (stable_key,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def harness_dream_changeset_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT c.* FROM harness_dream_generations g "
                "JOIN harness_dream_changesets c USING(stable_key) WHERE g.run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_harness_dream_changeset(self, source_identity: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE source_identity=? "
                "ORDER BY dream_date DESC,created_at DESC LIMIT 1",
                (source_identity,),
            ).fetchone()
        return dict(row) if row is not None else None

    def active_harness_dream_changesets(self) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE active_run_id IS NOT NULL "
                "ORDER BY dream_date,created_at",
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def release_orphan_harness_dream_generation(
        self, stable_key: str, *, expected_revision: int, run_id: str,
    ) -> dict[str, Any]:
        """Remove the only safe orphan: a claim committed before its Run existed."""
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM agent_states WHERE run_id=?", (run_id,),
            ).fetchone() is not None:
                raise StateConflictError("Harness Dream Run exists; orphan release is unsafe")
            changed = connection.execute(
                "UPDATE harness_dream_changesets SET status='discovered',active_run_id=NULL,"
                "revision=revision+1,updated_at=? WHERE stable_key=? AND revision=? "
                "AND active_run_id=? AND status='running'",
                (timestamp, stable_key, expected_revision, run_id),
            )
            if changed.rowcount != 1:
                raise StateConflictError("Harness Dream orphan release CAS failed")
            connection.execute(
                "UPDATE harness_dream_generations SET status='abandoned_before_run',"
                "result_json=?,completed_at=? WHERE stable_key=? AND run_id=?",
                (json.dumps({"reason": "run_not_created"}), timestamp, stable_key, run_id),
            )
            row = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            connection.commit()
        return dict(row)

    def freeze_harness_dream(
        self, source_identity: str, dream_date: str, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO harness_dream_freezes VALUES(?,?,?,?,?) "
                "ON CONFLICT(source_identity,dream_date) DO UPDATE SET "
                "actor=excluded.actor,reason=excluded.reason,created_at=excluded.created_at",
                (source_identity, dream_date, actor, reason, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM harness_dream_freezes WHERE source_identity=? AND dream_date=?",
                (source_identity, dream_date),
            ).fetchone()
            connection.commit()
        return dict(row)

    def unfreeze_harness_dream(self, source_identity: str, dream_date: str) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute(
                "DELETE FROM harness_dream_freezes WHERE source_identity=? AND dream_date=?",
                (source_identity, dream_date),
            ).rowcount
            connection.commit()
        return bool(removed)

    def harness_dream_freeze(self, source_identity: str, dream_date: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_dream_freezes WHERE source_identity=? AND dream_date=?",
                (source_identity, dream_date),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_harness_dream_revert(
        self, proposal_id: str, stable_key: str, operation_run_id: str,
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = now_iso()
        payload = json.dumps(
            AuditSanitizer.sanitize(proposal), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM harness_dream_reverts WHERE proposal_id=?", (proposal_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO harness_dream_reverts VALUES(?,?,?,?,?,0,?,?)",
                    (proposal_id, stable_key, operation_run_id, "proposed", payload,
                     timestamp, timestamp),
                )
                state = self._state_in(connection, operation_run_id)
                self._write_event(
                    connection, state, "harness_dream_revert_proposed",
                    {"proposal_id": proposal_id, "stable_key": stable_key},
                )
            elif str(existing["proposal_json"]) != payload:
                raise StateConflictError("Harness Dream revert proposal identity conflict")
            selected = connection.execute(
                "SELECT * FROM harness_dream_reverts WHERE proposal_id=?", (proposal_id,),
            ).fetchone()
            connection.commit()
        return dict(selected)

    def harness_dream_revert(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_dream_reverts WHERE proposal_id=?", (proposal_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def decide_harness_dream_revert(
        self, proposal_id: str, *, expected_revision: int, status: str,
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in {"approved", "rejected", "blocked", "merged"}:
            raise ValueError(f"Invalid Harness Dream revert status: {status}")
        timestamp = now_iso()
        payload = json.dumps(
            AuditSanitizer.sanitize(proposal), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE harness_dream_reverts SET status=?,proposal_json=?,revision=revision+1,"
                "updated_at=? WHERE proposal_id=? AND revision=?",
                (status, payload, timestamp, proposal_id, expected_revision),
            )
            if changed.rowcount != 1:
                raise StateConflictError("Harness Dream revert decision revision conflict")
            row = connection.execute(
                "SELECT * FROM harness_dream_reverts WHERE proposal_id=?", (proposal_id,),
            ).fetchone()
            state = self._state_in(connection, str(row["operation_run_id"]))
            self._write_event(
                connection, state, f"harness_dream_revert_{status}",
                {"proposal_id": proposal_id, "stable_key": str(row["stable_key"])},
            )
            connection.commit()
        return dict(row)

    def create_gateway_restart_request(
        self, request_id: str, stable_key: str, *, expected_pid: int,
        expected_gateway_epoch: str, expected_commit: str, request: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = now_iso()
        payload = json.dumps(
            AuditSanitizer.sanitize(request), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO gateway_restart_requests VALUES(?,?,?,?,?,'pending',?,?,?) "
                "ON CONFLICT(stable_key) DO NOTHING",
                (request_id, stable_key, expected_pid, expected_gateway_epoch,
                 expected_commit, payload, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM gateway_restart_requests WHERE stable_key=?", (stable_key,),
            ).fetchone()
            connection.commit()
        return dict(row)

    def update_gateway_restart_request(self, request_id: str, status: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE gateway_restart_requests SET status=?,updated_at=? WHERE request_id=?",
                (status, timestamp, request_id),
            )
            if changed.rowcount != 1:
                raise KeyError(request_id)
            row = connection.execute(
                "SELECT * FROM gateway_restart_requests WHERE request_id=?", (request_id,),
            ).fetchone()
            connection.commit()
        return dict(row)

    def pending_gateway_restart_requests(self) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_restart_requests WHERE status IN ('pending','waiting') "
                "ORDER BY created_at",
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def mark_harness_dream_restart_timeout(
        self, stable_key: str, request_id: str,
    ) -> dict[str, Any]:
        """Atomically stop automatic restart waiting and surface a durable alert."""
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            if row is None:
                raise KeyError(stable_key)
            run_id = str(row["active_run_id"] or "")
            if not run_id:
                generation = connection.execute(
                    "SELECT run_id FROM harness_dream_generations WHERE stable_key=? "
                    "ORDER BY generation DESC LIMIT 1", (stable_key,),
                ).fetchone()
                run_id = str(generation["run_id"]) if generation is not None else ""
            try:
                result_payload = json.loads(str(row["result_json"] or "{}"))
            except json.JSONDecodeError:
                result_payload = {}
            result_payload.update({
                "status": "restart_wait_timeout",
                "message": "Gateway restart safe-boundary wait timed out; restart manually",
            })
            result_json = json.dumps(
                result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            connection.execute(
                "UPDATE gateway_restart_requests SET status='restart_wait_timeout',updated_at=? "
                "WHERE request_id=?", (timestamp, request_id),
            )
            connection.execute(
                "UPDATE harness_dream_changesets SET status='restart_wait_timeout',"
                "result_json=?,revision=revision+1,updated_at=? WHERE stable_key=?",
                (result_json, timestamp, stable_key),
            )
            if run_id:
                connection.execute(
                    "UPDATE inbox SET title=?,summary=?,is_read=0 WHERE run_id=?",
                    ("Harness Dream restart timed out",
                     "Source merge succeeded, but safe automatic Gateway restart timed out. "
                     "Restart the Gateway manually.", run_id),
                )
                state = self._state_in(connection, run_id)
                self._write_event(
                    connection, state, "gateway_restart_wait_timeout",
                    {"stable_key": stable_key, "request_id": request_id,
                     "message": "Gateway restart safe-boundary wait timed out"},
                )
            selected = connection.execute(
                "SELECT * FROM harness_dream_changesets WHERE stable_key=?", (stable_key,),
            ).fetchone()
            connection.commit()
        return dict(selected)

    def operations(self, run_id: str) -> tuple[OperationRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM operation_ledger WHERE run_id=? ORDER BY updated_at,operation_id",
                (run_id,),
            ).fetchall()
        return tuple(OperationRecord.model_validate_json(row["record_json"], strict=True) for row in rows)

    def finalize_generation_invalidated(self, run_id: str, generation: int) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM finalize_generation_invalidations WHERE run_id=? AND generation=?",
                (run_id, generation),
            ).fetchone() is not None

    def transitions(self, run_id: str) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM state_transitions WHERE run_id=? ORDER BY revision,created_at",
                (run_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def recovery_decisions(self, run_id: str) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT decision_json FROM recovery_decisions WHERE run_id=? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return tuple(json.loads(row["decision_json"]) for row in rows)

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
            dead_letters = int(connection.execute(
                "SELECT COUNT(*) FROM event_outbox WHERE "
                "eventbus_dead_letter_at IS NOT NULL OR jsonl_dead_letter_at IS NOT NULL",
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
                "SELECT COUNT(*) FROM operation_attempts WHERE status='running' AND "
                "json_extract(attempt_json,'$.heartbeat_expires_at') IS NOT NULL AND "
                "json_extract(attempt_json,'$.heartbeat_expires_at')<=?",
                (timestamp.isoformat(timespec="seconds"),),
            ).fetchone()[0])
            oldest_state = connection.execute(
                "SELECT MIN(COALESCE((SELECT MAX(t.created_at) FROM state_transitions t "
                "WHERE t.run_id=a.run_id),json_extract(a.state_json,'$.created_at'))) "
                "FROM agent_states a WHERE task_state NOT IN "
                "('succeeded','failed','cancelled','interrupted')",
            ).fetchone()[0]
            oldest_heartbeat = connection.execute(
                "SELECT MIN(json_extract(attempt_json,'$.heartbeat_at')) FROM operation_attempts "
                "WHERE status='running'",
            ).fetchone()[0]
            dream_recovery = int(connection.execute(
                "SELECT COUNT(*) FROM harness_dream_changesets WHERE status='unknown'",
            ).fetchone()[0])
            restart_pending = int(connection.execute(
                "SELECT COUNT(*) FROM gateway_restart_requests WHERE status IN "
                "('pending','waiting','requested')",
            ).fetchone()[0])
        return {
            "sqlite_quick_check": quick_check,
            "outbox_backlog": outbox,
            "outbox_dead_letters": dead_letters,
            "recovery_required": recovery,
            "pending_approvals": pending,
            "expired_approvals": expired,
            "stalled_operations": stalled,
            "harness_dream_recovery_required": dream_recovery,
            "gateway_restart_pending": restart_pending,
            "max_state_age_seconds": _age_seconds(oldest_state, timestamp),
            "max_heartbeat_age_seconds": _age_seconds(oldest_heartbeat, timestamp),
            "migration_backup": str(self.migration_backup_path) if self.migration_backup_path else None,
        }

    def prune_retention(self, *, now: datetime | None = None) -> dict[str, int]:
        """Apply control-plane retention without deleting durable operation evidence."""
        selected = now or datetime.now().astimezone()
        idempotency_before = (selected - timedelta(days=30)).isoformat(timespec="seconds")
        history_before = (selected - timedelta(days=180)).isoformat(timespec="seconds")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            idempotency = connection.execute(
                "DELETE FROM idempotency_records WHERE updated_at<?", (idempotency_before,),
            ).rowcount
            transitions = connection.execute(
                "DELETE FROM state_transitions WHERE created_at<?", (history_before,),
            ).rowcount
            # Only fully delivered rows may be removed; dead letters remain until
            # an operator resolves them.
            removable = connection.execute(
                "SELECT event_id FROM event_outbox WHERE delivered_at IS NOT NULL "
                "AND delivered_at<?", (history_before,),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in removable]
            for event_id in event_ids:
                connection.execute("DELETE FROM event_outbox WHERE event_id=?", (event_id,))
                connection.execute("DELETE FROM gateway_events WHERE event_id=?", (event_id,))
            connection.commit()
        return {
            "idempotency_records": int(idempotency),
            "state_transitions": int(transitions),
            "gateway_events": len(event_ids),
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
        operation = OperationRecord(
            operation_id=command.operation_id,
            parent_operation_id=command.parent_operation_id,
            run_id=state.run_id,
            turn_id=command.turn_id,
            kind=command.kind,
            name=command.name,
            request_hash=command.request_hash,
            idempotency=command.idempotency,
            stable_key=f"legacy:{command.operation_id}",
            side_effecting=command.kind is OperationKind.TOOL and command.idempotency is not ToolIdempotency.PURE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        attempt = OperationAttempt(
            attempt_id=f"attempt:{command.operation_id}:1", operation_id=operation.operation_id,
            run_id=operation.run_id, attempt_no=1, request_hash=operation.request_hash,
            side_effecting=operation.side_effecting, created_at=timestamp, updated_at=timestamp,
        )
        self._save_operation(connection, operation)
        self._save_attempt(connection, attempt)
        return operation

    def _create_operation_with_attempt(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: CreateOperationWithAttemptCommand,
        timestamp: str,
    ) -> tuple[OperationRecord, OperationAttempt]:
        row = connection.execute(
            "SELECT record_json FROM operation_ledger WHERE run_id=? AND kind=? AND stable_key=?",
            (state.run_id, command.kind.value, command.stable_key),
        ).fetchone()
        if row is not None:
            operation = OperationRecord.model_validate_json(row["record_json"], strict=True)
            if (
                operation.request_hash != command.request_hash
                or operation.name != command.name
                or operation.side_effecting != command.side_effecting
                or operation.idempotency is not command.idempotency
                or operation.parent_operation_id != command.parent_operation_id
                or operation.logical_model_call_id != command.logical_model_call_id
                or operation.source_model_call_id != command.source_model_call_id
                or operation.tool_call_id != command.tool_call_id
                or operation.retry_policy_snapshot != command.retry_policy_snapshot
                or operation.external_idempotency_key != command.external_idempotency_key
            ):
                raise StateConflictError("相同 stable_key 不能重定义 Logical Operation 不可变元数据")
            return operation, self._current_attempt_in(connection, operation.operation_id)
        operation = OperationRecord(
            operation_id=command.operation_id, parent_operation_id=command.parent_operation_id,
            run_id=state.run_id, turn_id=command.turn_id, kind=command.kind, name=command.name,
            stable_key=command.stable_key, request_hash=command.request_hash,
            idempotency=command.idempotency, side_effecting=command.side_effecting,
            logical_model_call_id=command.logical_model_call_id,
            source_model_call_id=command.source_model_call_id, tool_call_id=command.tool_call_id,
            external_idempotency_key=command.external_idempotency_key,
            retry_policy_snapshot=command.retry_policy_snapshot,
            created_at=timestamp, updated_at=timestamp,
        )
        attempt = OperationAttempt(
            attempt_id=command.attempt_id, operation_id=command.operation_id, run_id=state.run_id,
            attempt_no=1, request_hash=command.request_hash, side_effecting=command.side_effecting,
            model_call_id=command.model_call_id, external_request_id=command.external_request_id,
            created_at=timestamp, updated_at=timestamp,
        )
        operation = self._aggregate_operation(operation, [attempt], timestamp)
        return operation, attempt

    def _begin_operation_attempt(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: BeginOperationAttemptCommand,
        timestamp: str,
    ) -> tuple[OperationRecord, OperationAttempt]:
        operation = self._operation_in(connection, command.operation_id)
        attempts = self._attempts_in(connection, operation.operation_id)
        latest = attempts[-1]
        if operation.run_id != state.run_id or operation.request_hash != command.request_hash:
            raise StateConflictError("Attempt request_hash 与 Logical Operation 不一致")
        if latest.attempt_no != command.expected_latest_attempt_no:
            raise StateConflictError("latest attempt CAS 冲突")
        if operation.status in {OperationStatus.COMPLETED, OperationStatus.SKIPPED}:
            raise StateInvariantError("已确定结束的 Operation 不能创建新 Attempt")
        if operation.status is OperationStatus.FAILED and operation.failure_kind is OperationFailureKind.TERMINAL:
            raise StateInvariantError("TERMINAL Operation 不能重试")
        if latest.status is OperationStatus.UNKNOWN and latest.recovery_resolution is not AttemptRecoveryResolution.RETRY_AUTHORIZED:
            raise StateInvariantError("UNKNOWN Attempt 未获人工授权，不能创建新 Attempt")
        if len(attempts) >= operation.retry_policy_snapshot.max_attempts:
            raise StateInvariantError("Operation 已达到 max_attempts")
        if operation.next_retry_at and datetime.fromisoformat(timestamp) < datetime.fromisoformat(operation.next_retry_at):
            raise StateInvariantError("尚未到达 next_retry_at")
        attempt = OperationAttempt(
            attempt_id=command.attempt_id, operation_id=operation.operation_id, run_id=state.run_id,
            attempt_no=latest.attempt_no + 1, request_hash=operation.request_hash,
            side_effecting=operation.side_effecting, model_call_id=command.model_call_id,
            external_request_id=command.external_request_id,
            risk_confirmed_by=command.risk_confirmed_by,
            risk_confirmation_reason=command.risk_confirmation_reason,
            created_at=timestamp, updated_at=timestamp,
        )
        operation = self._aggregate_operation(operation, [*attempts, attempt], timestamp)
        return operation, attempt

    def _update_attempt(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        attempt_id: str,
        timestamp: str,
        *,
        allowed: set[OperationStatus],
        updates: dict[str, Any],
    ) -> tuple[OperationRecord, OperationAttempt]:
        attempt = self._attempt_in(connection, attempt_id)
        if attempt.run_id != state.run_id or attempt.status not in allowed:
            raise StateInvariantError("Attempt 不属于当前 Run 或状态不允许该操作")
        attempt = OperationAttempt.model_validate(
            attempt.model_copy(update={**updates, "updated_at": timestamp}), strict=True,
        )
        operation = self._operation_in(connection, attempt.operation_id)
        attempts = [attempt if item.attempt_id == attempt_id else item
                    for item in self._attempts_in(connection, operation.operation_id)]
        operation = self._aggregate_operation(operation, attempts, timestamp)
        return operation, attempt

    def _reconcile_attempt(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: ReconcileOperationAttemptCommand,
        timestamp: str,
    ) -> tuple[OperationRecord, OperationAttempt]:
        result = command.result
        current = self._attempt_in(connection, command.attempt_id)
        common = {
            "reconcile_status": result.status,
            "reconcile_evidence": AuditSanitizer.sanitize(result.evidence),
            "result_source": result.result_source,
            "completed_at": timestamp,
        }
        if result.status is ReconcileStatus.COMPLETED:
            observed = result.observed_result or ""
            if current.status is OperationStatus.UNKNOWN:
                return self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.UNKNOWN},
                    updates={**common, "recovery_resolution": AttemptRecoveryResolution.CONFIRMED_SUCCEEDED,
                             "completed_at": None,
                             "result": AuditSanitizer.sanitize(observed),
                             "result_hash": hashlib.sha256(observed.encode("utf-8")).hexdigest()},
                )
            return self._update_attempt(
                connection, state, command.attempt_id, timestamp,
                allowed={OperationStatus.RUNNING, OperationStatus.UNKNOWN},
                updates={**common, "status": OperationStatus.COMPLETED, "failure_kind": None,
                         "failure_reason": None, "recovery_resolution": None,
                         "result": AuditSanitizer.sanitize(observed),
                         "result_hash": hashlib.sha256(observed.encode("utf-8")).hexdigest()},
            )
        if result.status is ReconcileStatus.NOT_APPLIED:
            return self._update_attempt(
                connection, state, command.attempt_id, timestamp,
                allowed={OperationStatus.RUNNING, OperationStatus.UNKNOWN},
                updates={**common, "status": OperationStatus.FAILED,
                         "failure_kind": OperationFailureKind.RETRYABLE,
                         "failure_reason": "reconcile_not_applied", "recovery_resolution": None},
            )
        if result.status is ReconcileStatus.FAILED:
            if current.status is OperationStatus.UNKNOWN:
                return self._update_attempt(
                    connection, state, command.attempt_id, timestamp,
                    allowed={OperationStatus.UNKNOWN},
                    updates={**common, "recovery_resolution": AttemptRecoveryResolution.CONFIRMED_FAILED,
                             "failure_reason": AuditSanitizer.sanitize(
                                 result.evidence or "reconcile_failed",
                             ), "completed_at": None},
                )
            return self._update_attempt(
                connection, state, command.attempt_id, timestamp,
                allowed={OperationStatus.RUNNING, OperationStatus.UNKNOWN},
                updates={**common, "status": OperationStatus.FAILED,
                         "failure_kind": OperationFailureKind.TERMINAL,
                         "failure_reason": AuditSanitizer.sanitize(result.evidence or "reconcile_failed"),
                         "recovery_resolution": None},
            )
        return self._update_attempt(
            connection, state, command.attempt_id, timestamp,
            allowed={OperationStatus.RUNNING, OperationStatus.UNKNOWN},
            updates={**common, "status": OperationStatus.UNKNOWN,
                     "failure_kind": OperationFailureKind.UNKNOWN_EFFECT,
                     "failure_reason": AuditSanitizer.sanitize(result.evidence or "reconcile_unknown"),
                     "recovery_resolution": AttemptRecoveryResolution.UNRESOLVED,
                     "completed_at": None},
        )

    @staticmethod
    def _aggregate_operation(
        operation: OperationRecord,
        attempts: list[OperationAttempt],
        timestamp: str,
    ) -> OperationRecord:
        aggregate = reduce_operation(
            operation.immutable_metadata(), attempts, operation.retry_policy_snapshot,
        )
        return operation.model_copy(update={
            **aggregate.model_dump(), "attempt": aggregate.latest_attempt_no,
            "started_at": attempts[-1].started_at, "heartbeat_at": attempts[-1].heartbeat_at,
            "heartbeat_expires_at": attempts[-1].heartbeat_expires_at,
            "completed_at": attempts[-1].completed_at, "error": aggregate.failure_reason,
            "unknown_reason": aggregate.failure_reason if aggregate.status is OperationStatus.UNKNOWN else None,
            "updated_at": timestamp,
        })

    def _apply_recovery(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: RecoveryDecisionCommand,
        timestamp: str,
    ) -> tuple[AgentState, OperationRecord | None, OperationAttempt | None]:
        operation = self._operation_in(connection, command.operation_id) if command.operation_id else None
        attempt = self._current_attempt_in(connection, operation.operation_id) if operation is not None else None
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
            if operation is None or attempt is None or attempt.status is not OperationStatus.UNKNOWN:
                raise StateInvariantError("只能人工确认 unknown Operation 成功")
            observed = command.observed_result or ""
            operation, attempt = self._update_attempt(
                connection, state, attempt.attempt_id, timestamp,
                allowed={OperationStatus.UNKNOWN},
                updates={
                "recovery_resolution": AttemptRecoveryResolution.CONFIRMED_SUCCEEDED,
                "result": AuditSanitizer.sanitize(observed),
                "result_hash": hashlib.sha256(observed.encode("utf-8")).hexdigest(),
                "result_source": "human_confirmed",
                "reconcile_evidence": f"actor={command.actor}; reason={command.reason}",
                "risk_confirmed_by": command.actor,
                "risk_confirmation_reason": AuditSanitizer.sanitize(command.reason),
            })
        elif (
            command.action == "retry"
            and operation is None
            and state.finalize_generation is not None
            and state.task_state is TaskState.RECOVERY_REQUIRED
        ):
            # Finalize generation recovery is coordinated after this durable
            # decision; no historical Operation/Attempt is mutated here.
            pass
        elif command.action == "retry":
            if operation is None or attempt is None or operation.status not in {
                OperationStatus.UNKNOWN, OperationStatus.FAILED,
            }:
                raise StateInvariantError("只能重试 unknown/failed Operation")
            if operation.idempotency is ToolIdempotency.NON_IDEMPOTENT and not command.risk_confirmed:
                raise StateInvariantError("NON_IDEMPOTENT Operation 重试必须人工确认风险")
            if attempt.status is OperationStatus.UNKNOWN:
                operation, attempt = self._update_attempt(
                    connection, state, attempt.attempt_id, timestamp,
                    allowed={OperationStatus.UNKNOWN},
                    updates={
                        "recovery_resolution": AttemptRecoveryResolution.RETRY_AUTHORIZED,
                        "risk_confirmed_by": command.actor,
                        "risk_confirmation_reason": AuditSanitizer.sanitize(command.reason),
                    },
                )
                # The resolution and its immutable evidence must be visible to the
                # attempt CAS that follows, while remaining in this SQLite transaction.
                self._save_attempt(connection, attempt)
                self._save_operation(connection, operation)
            operation, attempt = self._begin_operation_attempt(
                connection,
                state,
                BeginOperationAttemptCommand(
                    command_id=f"{command.command_id}:attempt",
                    run_id=command.run_id,
                    expected_revision=command.expected_revision,
                    gateway_epoch=command.gateway_epoch,
                    operation_id=operation.operation_id,
                    attempt_id=uuid4().hex,
                    expected_latest_attempt_no=operation.latest_attempt_no,
                    request_hash=operation.request_hash,
                    model_call_id=uuid4().hex if operation.kind is OperationKind.MODEL else None,
                    external_request_id=uuid4().hex if operation.kind is OperationKind.TOOL else None,
                    risk_confirmed_by=command.actor if command.risk_confirmed else None,
                    risk_confirmation_reason=command.reason if command.risk_confirmed else None,
                ),
                timestamp,
            )
        elif command.action == "fail" and operation is not None and attempt is not None:
            if attempt.status is OperationStatus.UNKNOWN:
                operation, attempt = self._update_attempt(
                    connection, state, attempt.attempt_id, timestamp,
                    allowed={OperationStatus.UNKNOWN},
                    updates={
                        "recovery_resolution": AttemptRecoveryResolution.CONFIRMED_FAILED,
                        "risk_confirmed_by": command.actor,
                        "risk_confirmation_reason": AuditSanitizer.sanitize(command.reason),
                    },
                )
            else:
                operation, attempt = self._update_attempt(
                    connection, state, attempt.attempt_id, timestamp,
                    allowed={OperationStatus.PREPARED, OperationStatus.RUNNING},
                    updates={
                        "status": OperationStatus.FAILED,
                        "failure_kind": OperationFailureKind.TERMINAL,
                        "failure_reason": AuditSanitizer.sanitize(command.reason),
                        "completed_at": timestamp,
                    },
                )
        elif command.action == "cancel":
            state = state.model_copy(update={"cancellation_requested": True})
        if operation is not None:
            self._save_operation(connection, operation)
        if attempt is not None:
                self._save_attempt(connection, attempt)
        connection.execute(
            "INSERT INTO recovery_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                uuid4().hex, command.command_id, state.run_id,
                operation.operation_id if operation else None,
                attempt.attempt_id if attempt else None,
                command.action, command.actor,
                AuditSanitizer.sanitize(command.reason),
                json.dumps(
                    AuditSanitizer.sanitize(command.model_dump(mode="json")),
                    ensure_ascii=False, sort_keys=True,
                ),
                timestamp,
            ),
        )
        return (
            state.model_copy(update={"diagnostics": diagnostic, "last_progress_at": timestamp}),
            operation,
            attempt,
        )

    @staticmethod
    def _insert_finalize_generation(
        connection: sqlite3.Connection,
        record: FinalizeGenerationRecord,
    ) -> None:
        connection.execute(
            "INSERT INTO finalize_generations"
            "(run_id,generation,protocol_version,generation_json,created_at) VALUES(?,?,?,?,?)",
            (
                record.run_id, record.generation, record.protocol_version,
                record.model_dump_json(), record.created_at,
            ),
        )

    @staticmethod
    def _canonical_hash(value: object) -> str:
        selected = AuditSanitizer.sanitize(value)
        serialized = json.dumps(
            selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _validate_finalize_evidence(
        cls,
        state: AgentState,
        operation: OperationRecord,
        attempt: OperationAttempt,
        result: str,
        result_hash: str,
    ) -> VerifiedArtifactEvidence | NotApplicableEvidence:
        try:
            evidence = FinalizeEvidenceCodec.verify(result, result_hash)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateInvariantError(f"Invalid Finalize Evidence: {exc}") from exc
        if evidence.run_id != state.run_id:
            raise StateInvariantError("Finalize Evidence run mismatch")
        if evidence.generation != state.finalize_generation:
            raise StateInvariantError("Finalize Evidence generation mismatch")
        if evidence.operation_id != operation.operation_id:
            raise StateInvariantError("Finalize Evidence operation mismatch")
        if evidence.attempt_id != attempt.attempt_id:
            raise StateInvariantError("Finalize Evidence attempt mismatch")
        expected = FinalizeIdentity.for_step(state, evidence.generation, evidence.step)
        if operation.stable_key != expected.stable_key or operation.request_hash != expected.request_hash:
            raise StateInvariantError("Finalize Evidence identity mismatch")
        try:
            FinalizeRequirementPolicy.validate(state, evidence)
        except ValueError as exc:
            raise StateInvariantError(str(exc)) from exc
        return evidence

    @classmethod
    def _completed_sqlite_finalize_operation(
        cls,
        state: AgentState,
        *,
        operation_id: str,
        attempt_id: str,
        stable_key: str,
        request_hash: str,
        kind: OperationKind,
        name: str,
        evidence_json: str,
        evidence_hash: str,
        timestamp: str,
    ) -> tuple[OperationRecord, OperationAttempt]:
        operation = OperationRecord(
            operation_id=operation_id, run_id=state.run_id, turn_id=state.turn_id,
            kind=kind, name=name, stable_key=stable_key, request_hash=request_hash,
            idempotency=ToolIdempotency.IDEMPOTENT, side_effecting=False,
            retry_policy_snapshot=RetryPolicySnapshot(
                max_attempts=1, base_seconds=0.0, max_seconds=0.0,
                automatic=False, requires_reconcile=False,
                requires_human_confirmation=False,
            ),
            created_at=timestamp, updated_at=timestamp,
        )
        attempt = OperationAttempt(
            attempt_id=attempt_id, operation_id=operation_id, run_id=state.run_id,
            attempt_no=1, request_hash=request_hash, side_effecting=False,
            status=OperationStatus.COMPLETED, result=evidence_json,
            result_hash=evidence_hash, result_source="sqlite_transaction",
            started_at=timestamp, completed_at=timestamp,
            created_at=timestamp, updated_at=timestamp,
        )
        return cls._aggregate_operation(operation, [attempt], timestamp), attempt

    def _finalize_audit(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: FinalizeAuditCommand,
        timestamp: str,
    ) -> tuple[OperationRecord, OperationAttempt]:
        if state.task_state is not TaskState.FINALIZING or state.finalize_generation != command.generation:
            raise StateInvariantError("Audit receipt requires the current FINALIZING generation")
        identity = FinalizeIdentity.for_step(state, command.generation, FinalizeStep.AUDIT)
        if (
            command.operation_id != identity.operation_id
            or command.stable_key != identity.stable_key
            or command.request_hash != identity.request_hash
        ):
            raise StateInvariantError("Audit finalize identity mismatch")
        try:
            receipt_value = json.loads(command.receipt_json)
        except json.JSONDecodeError as exc:
            raise StateInvariantError("Audit receipt is not valid JSON") from exc
        canonical_receipt = json.dumps(
            AuditSanitizer.sanitize(receipt_value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        if canonical_receipt != command.receipt_json:
            raise StateInvariantError("Audit receipt is not canonical or sanitized")
        if hashlib.sha256(command.receipt_json.encode("utf-8")).hexdigest() != command.receipt_hash:
            raise StateInvariantError("Audit receipt hash mismatch")
        encoded = FinalizeEvidenceCodec.encode(FinalizeEvidenceCodec.decode(command.evidence_json))
        if encoded.result_hash != command.evidence_hash:
            raise StateInvariantError("Audit Evidence hash mismatch")
        evidence = encoded.value
        if not isinstance(evidence, VerifiedArtifactEvidence):
            raise StateInvariantError("Audit Evidence must be verified")
        if evidence.step is not FinalizeStep.AUDIT or evidence.artifact_hash != command.receipt_hash:
            raise StateInvariantError("Audit Evidence does not reference the receipt")
        if evidence.references.receipt_id != command.receipt_id:
            raise StateInvariantError("Audit Evidence receipt_id mismatch")
        existing = connection.execute(
            "SELECT * FROM run_audit_receipts WHERE receipt_id=?", (command.receipt_id,),
        ).fetchone()
        values = (
            command.receipt_id, state.run_id, command.generation,
            command.receipt_generation, command.receipt_json, command.receipt_hash, timestamp,
        )
        if existing is None:
            connection.execute(
                "INSERT INTO run_audit_receipts VALUES(?,?,?,?,?,?,?)", values,
            )
        elif tuple(existing[key] for key in (
            "receipt_id", "run_id", "finalize_generation", "receipt_generation",
            "receipt_json", "receipt_hash", "created_at",
        )) != values:
            raise StateConflictError("Immutable audit receipt content conflict")
        connection.execute(
            "INSERT INTO run_current_audit_receipt(run_id,receipt_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET receipt_id=excluded.receipt_id,updated_at=excluded.updated_at",
            (state.run_id, command.receipt_id, timestamp),
        )
        operation, attempt = self._completed_sqlite_finalize_operation(
            state, operation_id=command.operation_id, attempt_id=command.attempt_id,
            stable_key=command.stable_key, request_hash=command.request_hash,
            kind=OperationKind.AUDIT, name="finalize_audit",
            evidence_json=encoded.serialized, evidence_hash=encoded.result_hash, timestamp=timestamp,
        )
        self._validate_finalize_evidence(
            state, operation, attempt, encoded.serialized, encoded.result_hash,
        )
        return operation, attempt

    def _finalize_inbox(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        command: FinalizeInboxCommand,
        timestamp: str,
    ) -> tuple[OperationRecord, OperationAttempt]:
        if state.task_state is not TaskState.FINALIZING or state.finalize_generation != command.generation:
            raise StateInvariantError("Inbox requires the current FINALIZING generation")
        identity = FinalizeIdentity.for_step(state, command.generation, FinalizeStep.INBOX)
        if (
            command.operation_id != identity.operation_id
            or command.stable_key != identity.stable_key
            or command.request_hash != identity.request_hash
        ):
            raise StateInvariantError("Inbox finalize identity mismatch")
        item_id = hashlib.sha256(
            f"{state.run_id}:finalize:v2:inbox".encode("utf-8"),
        ).hexdigest()[:32]
        title = command.title[:120]
        summary = AuditSanitizer.sanitize(command.summary)
        existing = connection.execute("SELECT * FROM inbox WHERE item_id=?", (item_id,)).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO inbox(item_id,run_id,project_id,session_id,title,summary,status,created_at,is_read) "
                "VALUES(?,?,?,?,?,?,?,?,0)",
                (
                    item_id, state.run_id, state.project_id, state.session_id,
                    title, summary, command.status, timestamp,
                ),
            )
            existing = connection.execute("SELECT * FROM inbox WHERE item_id=?", (item_id,)).fetchone()
        if existing is None:
            raise StateInvariantError("Inbox row was not durably created")
        expected = {
            "item_id": item_id, "run_id": state.run_id, "project_id": state.project_id,
            "session_id": state.session_id, "title": title, "summary": summary,
            "status": command.status, "created_at": str(existing["created_at"]),
        }
        actual = {key: existing[key] for key in expected}
        if actual != expected:
            raise StateConflictError("Deterministic Inbox item content conflict")
        artifact_hash = self._canonical_hash(expected)
        provisional = OperationAttempt(
            attempt_id=command.attempt_id, operation_id=command.operation_id,
            run_id=state.run_id, attempt_no=1, request_hash=command.request_hash,
            side_effecting=False, created_at=timestamp, updated_at=timestamp,
        )
        evidence = VerifiedArtifactEvidence(
            step=FinalizeStep.INBOX, run_id=state.run_id, generation=command.generation,
            operation_id=command.operation_id, attempt_id=command.attempt_id,
            artifact_kind="gateway_inbox_row", artifact_id=item_id,
            artifact_hash=artifact_hash, verification_method="sqlite_row_canonical_hash",
            references={"inbox_item_id": item_id},
        )
        encoded = FinalizeEvidenceCodec.encode(evidence)
        operation, attempt = self._completed_sqlite_finalize_operation(
            state, operation_id=command.operation_id, attempt_id=provisional.attempt_id,
            stable_key=command.stable_key, request_hash=command.request_hash,
            kind=OperationKind.INBOX, name="finalize_inbox",
            evidence_json=encoded.serialized, evidence_hash=encoded.result_hash, timestamp=timestamp,
        )
        self._validate_finalize_evidence(
            state, operation, attempt, encoded.serialized, encoded.result_hash,
        )
        return operation, attempt

    def _validate_terminal_finalize(
        self,
        connection: sqlite3.Connection,
        state: AgentState,
        generation: int,
    ) -> None:
        if state.finalize_generation != generation:
            raise StateInvariantError("FinalizeTerminalCommand generation mismatch")
        record = connection.execute(
            "SELECT 1 FROM finalize_generations WHERE run_id=? AND generation=?",
            (state.run_id, generation),
        ).fetchone()
        if record is None:
            raise StateInvariantError("Current finalize generation is missing")
        invalidated = connection.execute(
            "SELECT 1 FROM finalize_generation_invalidations WHERE run_id=? AND generation=?",
            (state.run_id, generation),
        ).fetchone()
        if invalidated is not None:
            raise StateInvariantError("Current finalize generation has been invalidated")
        evidence_by_step: dict[FinalizeStep, VerifiedArtifactEvidence | NotApplicableEvidence] = {}
        attempts_by_step: dict[FinalizeStep, OperationAttempt] = {}
        for step in FinalizeStep:
            identity = FinalizeIdentity.for_step(state, generation, step)
            row = connection.execute(
                "SELECT record_json FROM operation_ledger WHERE run_id=? AND kind=? AND stable_key=?",
                (state.run_id, _finalize_operation_kind(step).value, identity.stable_key),
            ).fetchone()
            if row is None:
                raise StateInvariantError(f"Missing Finalize Evidence: {step.value}")
            operation = OperationRecord.model_validate_json(row["record_json"], strict=True)
            if operation.operation_id != identity.operation_id or operation.status is not OperationStatus.COMPLETED:
                raise StateInvariantError(f"Finalize Operation is not completed: {step.value}")
            attempt = self._current_attempt_in(connection, operation.operation_id)
            if not attempt.result or not attempt.result_hash:
                raise StateInvariantError(f"Finalize Attempt lacks Evidence: {step.value}")
            evidence = self._validate_finalize_evidence(
                state, operation, attempt, attempt.result, attempt.result_hash,
            )
            evidence_by_step[step] = evidence
            attempts_by_step[step] = attempt
        index = evidence_by_step[FinalizeStep.SESSION_INDEX]
        memory = attempts_by_step[FinalizeStep.MEMORY]
        if isinstance(index, VerifiedArtifactEvidence):
            if (
                index.references.memory_attempt_id != memory.attempt_id
                or index.references.memory_result_hash != memory.result_hash
            ):
                raise StateInvariantError("Session Index Evidence does not reference Memory Evidence")
        current_receipt = connection.execute(
            "SELECT r.* FROM run_current_audit_receipt c "
            "JOIN run_audit_receipts r ON r.receipt_id=c.receipt_id WHERE c.run_id=?",
            (state.run_id,),
        ).fetchone()
        audit = evidence_by_step[FinalizeStep.AUDIT]
        index_attempt = attempts_by_step[FinalizeStep.SESSION_INDEX]
        if (
            current_receipt is None
            or int(current_receipt["finalize_generation"]) != generation
            or not isinstance(audit, VerifiedArtifactEvidence)
            or audit.references.receipt_id != current_receipt["receipt_id"]
            or audit.artifact_hash != current_receipt["receipt_hash"]
            or audit.references.memory_attempt_id != memory.attempt_id
            or audit.references.memory_result_hash != memory.result_hash
            or audit.references.session_index_attempt_id != index_attempt.attempt_id
            or audit.references.session_index_result_hash != index_attempt.result_hash
        ):
            raise StateInvariantError("Current audit receipt does not match Audit Evidence")
        inbox = evidence_by_step[FinalizeStep.INBOX]
        if not isinstance(inbox, VerifiedArtifactEvidence) or not inbox.references.inbox_item_id:
            raise StateInvariantError("Inbox Evidence is not verified")
        inbox_row = connection.execute(
            "SELECT * FROM inbox WHERE item_id=?", (inbox.references.inbox_item_id,),
        ).fetchone()
        if inbox_row is None:
            raise StateInvariantError("Inbox Evidence row is missing")
        canonical_inbox = {
            key: inbox_row[key]
            for key in (
                "item_id", "run_id", "project_id", "session_id", "title",
                "summary", "status", "created_at",
            )
        }
        if self._canonical_hash(canonical_inbox) != inbox.artifact_hash:
            raise StateInvariantError("Inbox Evidence row hash mismatch")

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
            UpgradePersistenceContractCommand: "persistence_contract_upgraded",
            StartFinalizeGenerationCommand: "finalize_generation_started",
            StartReplacementFinalizeGenerationCommand: "finalize_generation_replaced",
            InvalidateFinalizeGenerationCommand: "finalize_generation_invalidated",
            FinalizeAuditCommand: "finalize_audit_created",
            FinalizeInboxCommand: "inbox_created",
            FinalizeTerminalCommand: "run_terminal",
            BeginOperationCommand: "operation_prepared",
            CreateOperationWithAttemptCommand: "operation_created",
            BeginOperationAttemptCommand: "operation_attempt_prepared",
            StartOperationAttemptCommand: "operation_attempt_started",
            CompleteOperationAttemptCommand: "operation_attempt_completed",
            FailOperationAttemptCommand: "operation_attempt_failed",
            MarkOperationAttemptUnknownCommand: "operation_attempt_unknown",
            SkipOperationAttemptCommand: "operation_attempt_skipped",
            AbandonOperationAttemptCommand: "operation_attempt_abandoned",
            HeartbeatOperationAttemptCommand: "operation_attempt_heartbeat",
            ReconcileOperationAttemptCommand: "operation_attempt_reconciled",
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
        attempt: OperationAttempt | None,
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
        if attempt is not None:
            payload.update({
                "attempt_id": attempt.attempt_id,
                "attempt_no": attempt.attempt_no,
                "attempt_status": attempt.status.value,
                "model_call_id": attempt.model_call_id,
            })
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
            "INSERT INTO operation_ledger(operation_id,run_id,parent_operation_id,kind,status,record_json,updated_at,"
            "stable_key,request_hash,side_effecting) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(operation_id) DO UPDATE SET status=excluded.status,"
            "record_json=excluded.record_json,updated_at=excluded.updated_at",
            (
                operation.operation_id, operation.run_id, operation.parent_operation_id,
                operation.kind.value, operation.status.value, operation.model_dump_json(), operation.updated_at,
                operation.stable_key, operation.request_hash, int(operation.side_effecting),
            ),
        )

    @staticmethod
    def _save_attempt(connection: sqlite3.Connection, attempt: OperationAttempt) -> None:
        connection.execute(
            "INSERT INTO operation_attempts(attempt_id,operation_id,run_id,attempt_no,request_hash,side_effecting,"
            "status,failure_kind,recovery_resolution,attempt_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status,failure_kind=excluded.failure_kind,"
            "recovery_resolution=excluded.recovery_resolution,attempt_json=excluded.attempt_json,"
            "updated_at=excluded.updated_at",
            (
                attempt.attempt_id, attempt.operation_id, attempt.run_id, attempt.attempt_no,
                attempt.request_hash, int(attempt.side_effecting), attempt.status.value,
                attempt.failure_kind.value if attempt.failure_kind else None,
                attempt.recovery_resolution.value if attempt.recovery_resolution else None,
                attempt.model_dump_json(), attempt.updated_at,
            ),
        )

    @staticmethod
    def _save_approval(connection: sqlite3.Connection, approval: DurableApproval) -> None:
        connection.execute(
            "INSERT INTO durable_approvals(approval_id,run_id,operation_id,status,approval_json,expires_at,updated_at,attempt_id) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status,"
            "approval_json=excluded.approval_json,updated_at=excluded.updated_at",
            (
                approval.approval_id, approval.run_id, approval.operation_id,
                approval.status.value, approval.model_dump_json(), approval.expires_at,
                approval.decided_at or approval.created_at, approval.attempt_id,
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
    def _attempt_in(connection: sqlite3.Connection, attempt_id: str) -> OperationAttempt:
        row = connection.execute(
            "SELECT attempt_json FROM operation_attempts WHERE attempt_id=?", (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"未知 OperationAttempt：{attempt_id}")
        return OperationAttempt.model_validate_json(row["attempt_json"], strict=True)

    @staticmethod
    def _attempts_in(connection: sqlite3.Connection, operation_id: str) -> list[OperationAttempt]:
        rows = connection.execute(
            "SELECT attempt_json FROM operation_attempts WHERE operation_id=? ORDER BY attempt_no",
            (operation_id,),
        ).fetchall()
        if not rows:
            raise KeyError(f"Logical Operation 缺少 Attempt：{operation_id}")
        return [OperationAttempt.model_validate_json(row["attempt_json"], strict=True) for row in rows]

    @classmethod
    def _current_attempt_in(cls, connection: sqlite3.Connection, operation_id: str) -> OperationAttempt:
        attempts = cls._attempts_in(connection, operation_id)
        active = [item for item in attempts if item.status in {
            OperationStatus.PREPARED, OperationStatus.RUNNING,
        } or (
            item.status is OperationStatus.UNKNOWN
            and item.recovery_resolution is AttemptRecoveryResolution.UNRESOLVED
        )]
        return active[-1] if active else attempts[-1]

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

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _migrate_idempotency_scope(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(idempotency_records)").fetchall()
        }
        if {"client_id", "operation_name"}.issubset(columns):
            return
        rows = connection.execute("SELECT * FROM idempotency_records").fetchall()
        connection.execute("ALTER TABLE idempotency_records RENAME TO idempotency_records_legacy")
        connection.execute(
            "CREATE TABLE idempotency_records ("
            "client_id TEXT NOT NULL,operation_name TEXT NOT NULL,idempotency_key TEXT NOT NULL,"
            "request_hash TEXT NOT NULL,run_id TEXT NOT NULL,response_json TEXT,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "PRIMARY KEY(client_id,operation_name,idempotency_key))",
        )
        for row in rows:
            state_row = connection.execute(
                "SELECT state_json FROM agent_states WHERE run_id=?", (row["run_id"],),
            ).fetchone()
            if state_row is not None:
                state = AgentState.model_validate_json(state_row["state_json"], strict=True)
                client_id = state.client_id
                operation_name = state.workload_kind.value
            else:
                client_id, operation_name = "legacy", "legacy"
            connection.execute(
                "INSERT OR IGNORE INTO idempotency_records VALUES(?,?,?,?,?,?,?,?)",
                (client_id, operation_name, row["idempotency_key"], row["request_hash"],
                 row["run_id"], row["response_json"], row["created_at"], row["updated_at"]),
            )
        connection.execute("DROP TABLE idempotency_records_legacy")

    def _migrate_legacy_operations(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT operation_id,record_json FROM operation_ledger ORDER BY updated_at,operation_id",
        ).fetchall()
        for row in rows:
            operation = OperationRecord.model_validate_json(row["record_json"], strict=True)
            stable_key = operation.stable_key
            if stable_key == "legacy":
                stable_key = f"legacy:{operation.operation_id}"
                operation = operation.model_copy(update={"stable_key": stable_key})
                self._save_operation(connection, operation)
                connection.execute(
                    "UPDATE operation_ledger SET stable_key=?,request_hash=?,side_effecting=?,record_json=? "
                    "WHERE operation_id=?",
                    (operation.stable_key, operation.request_hash, int(operation.side_effecting),
                     operation.model_dump_json(), operation.operation_id),
                )
            existing = connection.execute(
                "SELECT 1 FROM operation_attempts WHERE operation_id=? LIMIT 1",
                (operation.operation_id,),
            ).fetchone()
            if existing is None:
                failure_kind = operation.failure_kind
                resolution = None
                failure_reason = operation.failure_reason or operation.error
                abandonment_reason = None
                if operation.status is OperationStatus.FAILED and failure_kind is None:
                    failure_kind = OperationFailureKind.TERMINAL
                if operation.status is OperationStatus.UNKNOWN:
                    failure_kind = OperationFailureKind.UNKNOWN_EFFECT
                    resolution = AttemptRecoveryResolution.UNRESOLVED
                    failure_reason = operation.unknown_reason or failure_reason or "legacy_unknown_effect"
                if operation.status is OperationStatus.ABANDONED:
                    abandonment_reason = operation.error or "legacy_abandoned"
                attempt = OperationAttempt(
                    attempt_id=f"legacy:{operation.operation_id}",
                    operation_id=operation.operation_id,
                    run_id=operation.run_id,
                    attempt_no=1,
                    request_hash=operation.request_hash,
                    side_effecting=operation.side_effecting,
                    status=operation.status,
                    failure_kind=failure_kind,
                    failure_reason=failure_reason,
                    recovery_resolution=resolution,
                    result=operation.result,
                    result_hash=operation.result_hash,
                    result_source=operation.result_source,
                    abandonment_reason=abandonment_reason,
                    started_at=operation.started_at,
                    heartbeat_at=operation.heartbeat_at,
                    heartbeat_expires_at=operation.heartbeat_expires_at,
                    completed_at=operation.completed_at,
                    created_at=operation.created_at,
                    updated_at=operation.updated_at,
                )
            if existing is None:
                self._save_attempt(connection, attempt)

    def _migrate_legacy_approvals(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT approval_id,operation_id,approval_json FROM durable_approvals",
        ).fetchall()
        for row in rows:
            payload = json.loads(row["approval_json"])
            if payload.get("attempt_id") and payload.get("stable_key") and payload.get("request_hash"):
                continue
            operation_row = connection.execute(
                "SELECT record_json FROM operation_ledger WHERE operation_id=?", (row["operation_id"],),
            ).fetchone()
            if operation_row is not None:
                operation = OperationRecord.model_validate_json(operation_row["record_json"], strict=True)
                attempt = self._current_attempt_in(connection, operation.operation_id)
                payload["attempt_id"] = attempt.attempt_id
                payload["attempt_no"] = attempt.attempt_no
                payload["stable_key"] = f"approval:{operation.stable_key}:{attempt.attempt_no}"
                payload["request_hash"] = operation.request_hash
            else:
                # Preserve the historical approval for audit. It cannot be decided
                # until an operator repairs its missing operation history.
                payload["attempt_id"] = f"legacy-missing:{row['approval_id']}"
                payload["attempt_no"] = 1
                payload["stable_key"] = f"approval:legacy-missing:{row['approval_id']}:1"
                payload["request_hash"] = "0" * 64
            connection.execute(
                "UPDATE durable_approvals SET attempt_id=?,approval_json=? WHERE approval_id=?",
                (payload["attempt_id"], json.dumps(payload, ensure_ascii=False), row["approval_id"]),
            )
            self._save_operation(connection, operation)

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


def _default_persistence_contract(workload_kind: WorkloadKind) -> PersistenceContract:
    if workload_kind is WorkloadKind.CHAT:
        return PersistenceContract.CONVERSATION_SESSION
    return PersistenceContract.CONTROL_ONLY


def _finalize_operation_kind(step: FinalizeStep) -> OperationKind:
    return {
        FinalizeStep.MEMORY: OperationKind.MEMORY,
        FinalizeStep.SESSION_INDEX: OperationKind.SESSION_INDEX,
        FinalizeStep.AUDIT: OperationKind.AUDIT,
        FinalizeStep.INBOX: OperationKind.INBOX,
    }[step]


def _age_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, (now - datetime.fromisoformat(value)).total_seconds())
    except ValueError:
        return None
