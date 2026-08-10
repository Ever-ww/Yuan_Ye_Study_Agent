"""Durable FINALIZING v2 orchestration built on the existing Operation ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from Agent.state import (
    AgentState,
    BeginOperationAttemptCommand,
    CompleteOperationAttemptCommand,
    CreateOperationWithAttemptCommand,
    ExecutionState,
    FailOperationAttemptCommand,
    FinalizeAuditCommand,
    FinalizeInboxCommand,
    FinalizeTerminalCommand,
    InvalidateFinalizeGenerationCommand,
    OperationAttempt,
    OperationFailureKind,
    OperationKind,
    OperationRecord,
    OperationStatus,
    PersistenceContract,
    RetryPolicySnapshot,
    StartFinalizeGenerationCommand,
    StartReplacementFinalizeGenerationCommand,
    StartOperationAttemptCommand,
    TaskState,
    ToolIdempotency,
    TransitionCommand,
)
from gateway.audit import AuditSanitizer
from gateway.finalize_evidence import (
    FinalizeEvidenceCodec,
    FinalizeEvidenceReferences,
    FinalizeIdentity,
    FinalizeRequirement,
    FinalizeRequirementPolicy,
    FinalizeStep,
    NotApplicableEvidence,
    VerifiedArtifactEvidence,
)
from gateway.session_reservation import SessionReservationRegistry
from gateway.state_controller import StateConflictError, StateController, StateInvariantError
from gateway.store import GatewayStore
from memory import MemoryStore


FINALIZE_RETRY_POLICY = RetryPolicySnapshot(
    max_attempts=3,
    base_seconds=1.0,
    max_seconds=10.0,
    automatic=True,
    requires_reconcile=False,
    requires_human_confirmation=False,
)


class FinalizeEvidenceConflict(RuntimeError):
    pass


class OperationRetryDriver:
    """Wake durable retries without owning retry policy or attempt state."""

    def __init__(self, controller: StateController) -> None:
        self.controller = controller

    async def wait_until_eligible(
        self,
        run_id: str,
        operation_id: str,
        generation: int,
    ) -> OperationAttempt:
        while True:
            state = self.controller.state(run_id)
            operation = self.controller.operation(operation_id)
            if state.finalize_generation != generation:
                raise FinalizeEvidenceConflict("Finalize generation changed while waiting for retry")
            if operation.status is not OperationStatus.FAILED:
                raise FinalizeEvidenceConflict("Finalize retry requested for a non-failed Operation")
            if operation.failure_kind is OperationFailureKind.TERMINAL:
                raise FinalizeEvidenceConflict(operation.failure_reason or "Finalize retry exhausted")
            if not operation.next_retry_at or not operation.retry_policy_snapshot.automatic:
                raise FinalizeEvidenceConflict("Finalize Operation is not automatically retryable")
            retry_at = datetime.fromisoformat(operation.next_retry_at.replace("Z", "+00:00"))
            delay = max(0.0, (retry_at - datetime.now().astimezone()).total_seconds())
            if delay:
                await asyncio.sleep(delay)
                continue
            current = self.controller.state(run_id)
            try:
                result = self.controller.apply(BeginOperationAttemptCommand(
                    command_id=(
                        f"{run_id}:{operation.stable_key}:attempt:"
                        f"{operation.latest_attempt_no + 1}:prepare"
                    ),
                    run_id=run_id,
                    expected_revision=current.revision,
                    gateway_epoch=self.controller.gateway_epoch,
                    operation_id=operation.operation_id,
                    attempt_id=uuid4().hex,
                    expected_latest_attempt_no=operation.latest_attempt_no,
                    request_hash=operation.request_hash,
                    external_request_id=(uuid4().hex if operation.side_effecting else None),
                ))
            except StateConflictError:
                await asyncio.sleep(0)
                continue
            if result.attempt is None:
                raise RuntimeError("BeginOperationAttemptCommand did not return an Attempt")
            return result.attempt


class FinalizeCoordinator:
    """Order finalize steps while StateController remains the only mutation gateway."""

    def __init__(
        self,
        *,
        controller: StateController,
        store: GatewayStore,
        agent_root: Path,
        reservations: SessionReservationRegistry,
        retry_driver: OperationRetryDriver | None = None,
    ) -> None:
        self.controller = controller
        self.store = store
        self.agent_root = agent_root.resolve()
        self.reservations = reservations
        self.retry_driver = retry_driver or OperationRetryDriver(controller)

    async def finalize(self, run_id: str) -> AgentState:
        state = self.controller.state(run_id)
        if state.task_state is not TaskState.FINALIZING:
            raise RuntimeError("FinalizeCoordinator requires a FINALIZING Run")
        if state.session_id:
            async with self.reservations.reserve(
                state.project_id, state.session_id, owner_id=run_id, wait=True,
            ):
                return await self._finalize_reserved(run_id)
        return await self._finalize_reserved(run_id)

    async def recover_generation(self, run_id: str, reason: str) -> AgentState:
        state = self.controller.state(run_id)
        if state.task_state is not TaskState.RECOVERY_REQUIRED or state.finalize_generation is None:
            raise RuntimeError("Run has no recoverable Finalize generation")
        old_generation = state.finalize_generation
        state = self.controller.apply(TransitionCommand(
            command_id=f"finalize:v2:{run_id}:g{old_generation}:recovering",
            run_id=run_id, expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            task_state=TaskState.RECOVERING,
            reason=f"Recover Finalize generation {old_generation}: {reason}",
        )).state
        state = self.controller.apply(TransitionCommand(
            command_id=f"finalize:v2:{run_id}:g{old_generation}:resume-finalizing",
            run_id=run_id, expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            task_state=TaskState.FINALIZING,
            terminal_target=state.terminal_target,
            reason="Resume durable FINALIZING",
        )).state
        if self.controller.finalize_generation_invalidated(run_id, old_generation):
            state = self.controller.apply(StartReplacementFinalizeGenerationCommand(
                command_id=f"finalize:v2:{run_id}:generation:{old_generation + 1}",
                run_id=run_id, expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                generation=old_generation + 1,
                supersedes_generation=old_generation,
                invalidated_generation=old_generation,
                reason=reason,
            )).state
        return await self.finalize(run_id)

    async def _finalize_reserved(self, run_id: str) -> AgentState:
        state = self.controller.state(run_id)
        if state.finalize_generation is None:
            result = self.controller.apply(StartFinalizeGenerationCommand(
                command_id=f"finalize:v2:{run_id}:generation:1",
                run_id=run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                generation=1,
            ))
            state = result.state
        generation = state.finalize_generation
        if generation is None:  # guarded by StartFinalizeGenerationCommand
            raise StateInvariantError("Finalize generation was not persisted")
        try:
            memory = await self._ensure_file_step(run_id, generation, FinalizeStep.MEMORY)
            index = await self._ensure_file_step(run_id, generation, FinalizeStep.SESSION_INDEX)
            audit = self._ensure_audit(run_id, generation, memory, index)
            self._ensure_inbox(run_id, generation)
            state = self.controller.state(run_id)
            result = self.controller.apply(FinalizeTerminalCommand(
                command_id=f"finalize:v2:{run_id}:g{generation}:terminal",
                run_id=run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                generation=generation,
                reason=f"Finalize generation {generation} durable evidence verified",
            ))
            del audit
            return result.state
        except StateInvariantError as exc:
            self._invalidate_generation(run_id, generation, str(exc) or type(exc).__name__)
            self._enter_recovery_required(run_id, str(exc) or type(exc).__name__)
            return self.controller.state(run_id)
        except (FinalizeEvidenceConflict, KeyError, OSError, ValueError) as exc:
            # An escaped step error means the current generation can no longer
            # reach Terminal (for example retry exhaustion or an artifact
            # conflict).  Recovery must preserve it and prove a new generation.
            self._invalidate_generation(run_id, generation, str(exc) or type(exc).__name__)
            self._enter_recovery_required(run_id, str(exc) or type(exc).__name__)
            return self.controller.state(run_id)

    async def _ensure_file_step(
        self,
        run_id: str,
        generation: int,
        step: FinalizeStep,
    ) -> OperationRecord:
        state = self.controller.state(run_id)
        identity = FinalizeIdentity.for_step(state, generation, step)
        operation = self._operation_for_stable_key(run_id, identity.stable_key)
        if operation is not None and operation.status is OperationStatus.COMPLETED:
            self._revalidate_completed_file_step(state, generation, step, operation)
            return operation
        if operation is None:
            result = self.controller.apply(CreateOperationWithAttemptCommand(
                command_id=FinalizeIdentity.command_id(run_id, generation, step, "create"),
                run_id=run_id,
                expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                operation_id=identity.operation_id,
                attempt_id=uuid4().hex,
                turn_id=state.turn_id,
                kind=_operation_kind(step),
                name=f"finalize_{step.value}",
                stable_key=identity.stable_key,
                request_hash=identity.request_hash,
                idempotency=ToolIdempotency.IDEMPOTENT,
                side_effecting=(
                    FinalizeRequirementPolicy.requirements(state)[step]
                    is FinalizeRequirement.REQUIRED
                ),
                external_request_id=uuid4().hex,
                external_idempotency_key=identity.stable_key,
                retry_policy_snapshot=FINALIZE_RETRY_POLICY,
            ))
            operation = result.operation
            attempt = result.attempt
        else:
            attempt = self.controller.current_attempt(operation.operation_id)
            if operation.status is OperationStatus.FAILED:
                attempt = await self.retry_driver.wait_until_eligible(
                    run_id, operation.operation_id, generation,
                )
        if operation is None or attempt is None:
            raise RuntimeError("Finalize Operation/Attempt was not created")
        if attempt.status is OperationStatus.PREPARED:
            current = self.controller.state(run_id)
            started = self.controller.apply(StartOperationAttemptCommand(
                command_id=_attempt_command_id(identity, attempt, "start"),
                run_id=run_id,
                expected_revision=current.revision,
                gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt.attempt_id,
            ))
            attempt = started.attempt
        if attempt is None or attempt.status is not OperationStatus.RUNNING:
            raise FinalizeEvidenceConflict(f"Finalize {step.value} Attempt is not runnable")
        try:
            evidence = self._build_file_evidence(
                self.controller.state(run_id), generation, step, operation, attempt,
            )
            encoded = FinalizeEvidenceCodec.encode(evidence)
            current = self.controller.state(run_id)
            completed = self.controller.apply(CompleteOperationAttemptCommand(
                command_id=_attempt_command_id(identity, attempt, "complete"),
                run_id=run_id,
                expected_revision=current.revision,
                gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt.attempt_id,
                result=encoded.serialized,
                result_hash=encoded.result_hash,
                result_source="finalize_artifact_verification",
            ))
            if completed.operation is None:
                raise StateInvariantError("Finalize completion did not return an Operation")
            return completed.operation
        except FinalizeEvidenceConflict:
            current = self.controller.state(run_id)
            self.controller.apply(FailOperationAttemptCommand(
                command_id=_attempt_command_id(identity, attempt, "conflict"),
                run_id=run_id,
                expected_revision=current.revision,
                gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt.attempt_id,
                failure_kind=OperationFailureKind.TERMINAL,
                failure_reason=f"{step.value}_evidence_conflict",
            ))
            raise
        except (OSError, ValueError) as exc:
            current = self.controller.state(run_id)
            failed = self.controller.apply(FailOperationAttemptCommand(
                command_id=_attempt_command_id(identity, attempt, "failed"),
                run_id=run_id,
                expected_revision=current.revision,
                gateway_epoch=self.controller.gateway_epoch,
                attempt_id=attempt.attempt_id,
                failure_kind=OperationFailureKind.RETRYABLE,
                failure_reason=str(exc) or type(exc).__name__,
            ))
            if failed.operation is None or failed.operation.failure_kind is OperationFailureKind.TERMINAL:
                raise FinalizeEvidenceConflict(str(exc) or type(exc).__name__) from exc
            return await self._ensure_file_step(run_id, generation, step)

    def _revalidate_completed_file_step(
        self,
        state: AgentState,
        generation: int,
        step: FinalizeStep,
        operation: OperationRecord,
    ) -> None:
        """Re-prove an external artifact without changing immutable Attempt history."""
        attempt = self.controller.current_attempt(operation.operation_id)
        if not attempt.result or not attempt.result_hash:
            raise StateInvariantError(f"Completed Finalize {step.value} lacks Evidence")
        try:
            persisted = FinalizeEvidenceCodec.verify(attempt.result, attempt.result_hash)
            current = self._build_file_evidence(state, generation, step, operation, attempt)
            encoded = FinalizeEvidenceCodec.encode(current)
        except (FinalizeEvidenceConflict, OSError, ValueError) as exc:
            raise StateInvariantError(
                f"Completed Finalize {step.value} Evidence cannot be re-proven: {exc}",
            ) from exc
        if encoded.result_hash != attempt.result_hash or encoded.value != persisted:
            raise StateInvariantError(
                f"Completed Finalize {step.value} Evidence no longer matches its artifact",
            )

    def _build_file_evidence(
        self,
        state: AgentState,
        generation: int,
        step: FinalizeStep,
        operation: OperationRecord,
        attempt: OperationAttempt,
    ) -> VerifiedArtifactEvidence | NotApplicableEvidence:
        requirement = FinalizeRequirementPolicy.requirements(state)[step]
        if requirement is FinalizeRequirement.NOT_APPLICABLE:
            return NotApplicableEvidence(
                step=step, run_id=state.run_id, generation=generation,
                operation_id=operation.operation_id,
                attempt_id=attempt.attempt_id,
                workload_kind=state.workload_kind.value,
                persistence_contract=state.persistence_contract,
                policy_reason="workload persistence contract has no Session artifact",
            )
        if not state.session_id:
            raise FinalizeEvidenceConflict("Session-backed Run has no bound session_id")
        project = self.store.project(state.project_id)
        memory = MemoryStore(
            self.agent_root / ".yy" / "memory",
            workspace_root=Path(project.path),
            agent_root=self.agent_root,
        )
        if step is FinalizeStep.MEMORY:
            records = memory.sessions.find_records_strict(
                state.session_id, run_id=state.run_id, turn_id=state.turn_id,
            )
            if not records:
                raise FinalizeEvidenceConflict("No original Session records found for Run/Turn")
            _validate_session_turn_chain(records)
            if records[-1][1].role != "assistant":
                raise FinalizeEvidenceConflict("Run/Turn has no terminal assistant Session record")
            canonical = [
                record.model_dump(mode="json", exclude_none=True)
                for _, record in records
            ]
            artifact_hash = _canonical_hash(canonical)
            segments = tuple(dict.fromkeys(filename for filename, _ in records))
            return VerifiedArtifactEvidence(
                step=step, run_id=state.run_id, generation=generation,
                operation_id=operation.operation_id, attempt_id=attempt.attempt_id,
                artifact_kind="session_record_chain",
                artifact_id=f"{state.session_id}:{state.run_id}:{state.turn_id or 'none'}",
                artifact_hash=artifact_hash,
                verification_method="strict_original_session_records",
                references=FinalizeEvidenceReferences(
                    segments=segments,
                    first_record_id=records[0][1].record_id,
                    last_record_id=records[-1][1].record_id,
                    final_record_id=records[-1][1].record_id,
                    record_count=len(records),
                ),
            )
        memory_operation = self._required_completed_operation(
            state, generation, FinalizeStep.MEMORY,
        )
        memory_attempt = self.controller.current_attempt(memory_operation.operation_id)
        if not memory_attempt.result or not memory_attempt.result_hash:
            raise FinalizeEvidenceConflict("Memory prerequisite lacks durable Evidence")
        memory_evidence = FinalizeEvidenceCodec.verify(
            memory_attempt.result, memory_attempt.result_hash,
        )
        if not isinstance(memory_evidence, VerifiedArtifactEvidence):
            raise FinalizeEvidenceConflict("Session Index requires verified Memory Evidence")
        entry = memory.sessions.index_entry(state.session_id)
        files = tuple(str(item) for item in entry["files"])
        if str(entry["latest_file"]) not in files:
            raise FinalizeEvidenceConflict("Session latest_file is not present in files")
        if any(segment not in files for segment in memory_evidence.references.segments):
            raise FinalizeEvidenceConflict("Memory Evidence segment is missing from Session index")
        if any(not (memory.sessions.directory / filename).is_file() for filename in files):
            raise FinalizeEvidenceConflict("Session index references a missing segment")
        artifact = {
            "session_id": state.session_id,
            "created_at": entry["created_at"],
            "files": files,
            "latest_file": entry["latest_file"],
            "final_record_id": memory_evidence.references.final_record_id,
            "final_record_segments": memory_evidence.references.segments,
        }
        return VerifiedArtifactEvidence(
            step=step, run_id=state.run_id, generation=generation,
            operation_id=operation.operation_id, attempt_id=attempt.attempt_id,
            artifact_kind="session_index",
            artifact_id=state.session_id,
            artifact_hash=_canonical_hash(artifact),
            verification_method="strict_index_and_segment_relationship",
            references=FinalizeEvidenceReferences(
                segments=files,
                final_record_id=memory_evidence.references.final_record_id,
                memory_attempt_id=memory_attempt.attempt_id,
                memory_result_hash=memory_attempt.result_hash,
            ),
        )

    def _ensure_audit(
        self,
        run_id: str,
        generation: int,
        memory: OperationRecord,
        index: OperationRecord,
    ) -> OperationRecord:
        state = self.controller.state(run_id)
        identity = FinalizeIdentity.for_step(state, generation, FinalizeStep.AUDIT)
        existing = self._operation_for_stable_key(run_id, identity.stable_key)
        if existing is not None:
            if existing.status is not OperationStatus.COMPLETED:
                raise FinalizeEvidenceConflict("Audit finalize Operation is incomplete")
            return existing
        memory_attempt = self.controller.current_attempt(memory.operation_id)
        index_attempt = self.controller.current_attempt(index.operation_id)
        receipt_generation = generation
        receipt = AuditSanitizer.sanitize({
            "protocol_version": 2,
            "run_id": run_id,
            "finalize_generation": generation,
            "receipt_generation": receipt_generation,
            "terminal_target": state.terminal_target.value if state.terminal_target else None,
            "execution_outcome": (
                state.execution.outcome.value
                if state.execution and state.execution.outcome else None
            ),
            "finish_reason": state.execution.finish_reason if state.execution else None,
            "memory_attempt_id": memory_attempt.attempt_id,
            "memory_result_hash": memory_attempt.result_hash,
            "session_index_attempt_id": index_attempt.attempt_id,
            "session_index_result_hash": index_attempt.result_hash,
        })
        receipt_json = _canonical_json(receipt)
        receipt_hash = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        receipt_id = hashlib.sha256(
            f"{run_id}:audit:g{generation}:{receipt_hash}".encode("utf-8"),
        ).hexdigest()
        attempt_id = uuid4().hex
        evidence = VerifiedArtifactEvidence(
            step=FinalizeStep.AUDIT, run_id=run_id, generation=generation,
            operation_id=identity.operation_id, attempt_id=attempt_id,
            artifact_kind="run_audit_receipt", artifact_id=receipt_id,
            artifact_hash=receipt_hash, verification_method="immutable_sqlite_receipt",
            references=FinalizeEvidenceReferences(
                memory_attempt_id=memory_attempt.attempt_id,
                memory_result_hash=memory_attempt.result_hash,
                session_index_attempt_id=index_attempt.attempt_id,
                session_index_result_hash=index_attempt.result_hash,
                receipt_id=receipt_id,
            ),
        )
        encoded = FinalizeEvidenceCodec.encode(evidence)
        result = self.controller.apply(FinalizeAuditCommand(
            command_id=FinalizeIdentity.command_id(run_id, generation, FinalizeStep.AUDIT, "commit"),
            run_id=run_id, expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=identity.operation_id, attempt_id=attempt_id,
            generation=generation, stable_key=identity.stable_key,
            request_hash=identity.request_hash, receipt_id=receipt_id,
            receipt_generation=receipt_generation, receipt_json=receipt_json,
            receipt_hash=receipt_hash, evidence_json=encoded.serialized,
            evidence_hash=encoded.result_hash,
        ))
        if result.operation is None:
            raise StateInvariantError("Audit transaction did not return an Operation")
        return result.operation

    def _ensure_inbox(self, run_id: str, generation: int) -> OperationRecord:
        state = self.controller.state(run_id)
        identity = FinalizeIdentity.for_step(state, generation, FinalizeStep.INBOX)
        existing = self._operation_for_stable_key(run_id, identity.stable_key)
        if existing is not None:
            if existing.status is not OperationStatus.COMPLETED:
                raise FinalizeEvidenceConflict("Inbox finalize Operation is incomplete")
            return existing
        target = state.terminal_target
        if target is None:
            raise FinalizeEvidenceConflict("FINALIZING state has no terminal_target")
        status = {
            "succeeded": "completed", "failed": "failed", "cancelled": "cancelled",
        }[target.value]
        run = self.store.run(run_id)
        result = self.controller.apply(FinalizeInboxCommand(
            command_id=FinalizeIdentity.command_id(run_id, generation, FinalizeStep.INBOX, "commit"),
            run_id=run_id, expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            operation_id=identity.operation_id, attempt_id=uuid4().hex,
            generation=generation, stable_key=identity.stable_key,
            request_hash=identity.request_hash,
            title=run.task[:120] or "Agent task",
            summary=state.result_summary or state.error or "",
            status=status,
        ))
        if result.operation is None:
            raise StateInvariantError("Inbox transaction did not return an Operation")
        return result.operation

    def _required_completed_operation(
        self,
        state: AgentState,
        generation: int,
        step: FinalizeStep,
    ) -> OperationRecord:
        identity = FinalizeIdentity.for_step(state, generation, step)
        operation = self._operation_for_stable_key(state.run_id, identity.stable_key)
        if operation is None or operation.status is not OperationStatus.COMPLETED:
            raise FinalizeEvidenceConflict(f"Finalize prerequisite is incomplete: {step.value}")
        return operation

    def _operation_for_stable_key(self, run_id: str, stable_key: str) -> OperationRecord | None:
        return next(
            (item for item in self.controller.operations(run_id) if item.stable_key == stable_key),
            None,
        )

    def _enter_recovery_required(self, run_id: str, reason: str) -> None:
        state = self.controller.state(run_id)
        if state.task_state is TaskState.RECOVERY_REQUIRED:
            return
        self.controller.apply(TransitionCommand(
            command_id=f"finalize:v2:{run_id}:recovery:{state.revision}",
            run_id=run_id, expected_revision=state.revision,
            gateway_epoch=self.controller.gateway_epoch,
            task_state=TaskState.RECOVERY_REQUIRED,
            reason=f"FINALIZING durable evidence requires recovery: {reason}",
        ))

    def _invalidate_generation(self, run_id: str, generation: int, reason: str) -> None:
        state = self.controller.state(run_id)
        if state.finalize_generation != generation:
            return
        try:
            self.controller.apply(InvalidateFinalizeGenerationCommand(
                command_id=f"finalize:v2:{run_id}:g{generation}:invalidate",
                run_id=run_id, expected_revision=state.revision,
                gateway_epoch=self.controller.gateway_epoch,
                generation=generation, reason=reason,
            ))
        except StateConflictError:
            return


def _operation_kind(step: FinalizeStep) -> OperationKind:
    return {
        FinalizeStep.MEMORY: OperationKind.MEMORY,
        FinalizeStep.SESSION_INDEX: OperationKind.SESSION_INDEX,
        FinalizeStep.AUDIT: OperationKind.AUDIT,
        FinalizeStep.INBOX: OperationKind.INBOX,
    }[step]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _validate_session_turn_chain(records: list[tuple[str, object]]) -> None:
    """Validate the persisted provider tool protocol, never a repaired prompt projection."""
    pending: dict[str, str] = {}
    for _, raw in records:
        role = getattr(raw, "role", None)
        calls = getattr(raw, "tool_calls", None)
        if role == "assistant" and calls:
            if pending:
                raise FinalizeEvidenceConflict("Session has an unfinished tool-call block")
            for call in calls:
                if not isinstance(call, dict):
                    raise FinalizeEvidenceConflict("Session contains an invalid tool call")
                call_id = str(call.get("id") or "")
                function = call.get("function")
                name = str(function.get("name") or "") if isinstance(function, dict) else ""
                if not call_id or not name or call_id in pending:
                    raise FinalizeEvidenceConflict("Session contains an invalid tool-call identity")
                pending[call_id] = name
            continue
        if role == "tool":
            call_id = str(getattr(raw, "tool_call_id", None) or "")
            name = str(getattr(raw, "name", None) or "")
            if call_id not in pending or pending[call_id] != name:
                raise FinalizeEvidenceConflict("Session contains an orphan or mismatched tool result")
            pending.pop(call_id)
            continue
        if pending:
            raise FinalizeEvidenceConflict("Session tool-call block is missing a persisted result")
    if pending:
        raise FinalizeEvidenceConflict("Session ends with an unfinished tool-call block")


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _attempt_command_id(identity, attempt: OperationAttempt, action: str) -> str:
    return (
        f"{identity.run_id}:{identity.stable_key}:"
        f"attempt:{attempt.attempt_no}:{attempt.attempt_id}:{action}"
    )


__all__ = [
    "FINALIZE_RETRY_POLICY",
    "FinalizeCoordinator",
    "FinalizeEvidenceConflict",
    "OperationRetryDriver",
]
