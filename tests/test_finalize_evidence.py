from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from Agent.state import (
    ExecutionOutcome,
    ExecutionState,
    FinalizeTerminalCommand,
    OperationStatus,
    StartFinalizeGenerationCommand,
    TaskState,
    TerminalTarget,
    TransitionCommand,
    WorkloadKind,
)
from gateway.finalize import FinalizeCoordinator
from gateway.finalize_evidence import (
    FinalizeEvidenceCodec,
    FinalizeIdentity,
    FinalizeStep,
    VerifiedArtifactEvidence,
)
from gateway.session_reservation import SessionReservationConflict, SessionReservationRegistry
from gateway.state_controller import StateController, StateInvariantError
from gateway.store import GatewayStore
from memory import MemoryStore


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _transition(controller: StateController, run_id: str, **changes):
    state = controller.state(run_id)
    return controller.apply(TransitionCommand(
        command_id=uuid4().hex,
        run_id=run_id,
        expected_revision=state.revision,
        gateway_epoch=controller.gateway_epoch,
        reason="test",
        **changes,
    )).state


def _conversation(tmp_path: Path, *, split_segments: bool = False):
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    project = store.register_project(tmp_path)
    controller = StateController(store.database_path, gateway_epoch="epoch")
    session_id = "a" * 16
    memory = MemoryStore(
        tmp_path / ".yy" / "memory", workspace_root=tmp_path, agent_root=tmp_path,
    )
    memory.create_session("task", session_id=session_id)
    state, _ = controller.create_run(
        run_id=uuid4().hex,
        workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id,
        client_id="client",
        task="task",
        idempotency_key=uuid4().hex,
        request_hash=_hash("task"),
        session_id=session_id,
    )
    state = _transition(controller, state.run_id, task_state=TaskState.QUEUED)
    state = _transition(controller, state.run_id, task_state=TaskState.STARTING)
    state = _transition(
        controller, state.run_id,
        task_state=TaskState.RUNNING,
        execution_state=ExecutionState.THINKING,
    )
    audit = {"run_id": state.run_id, "turn_id": state.turn_id}
    memory.record_user(session_id, "question", audit=audit)
    if split_segments:
        memory.sessions.start_new_segment(session_id)
    memory.record_assistant(session_id, "answer", audit=audit)
    state = _transition(
        controller, state.run_id,
        execution_state=ExecutionState.FINISHED,
        outcome=ExecutionOutcome.SUCCESS,
        finish_reason="done",
    )
    state = _transition(
        controller, state.run_id,
        task_state=TaskState.FINALIZING,
        terminal_target=TerminalTarget.SUCCEEDED,
        result_summary="answer",
    )
    reservations = SessionReservationRegistry()
    finalizer = FinalizeCoordinator(
        controller=controller, store=store, agent_root=tmp_path,
        reservations=reservations,
    )
    return store, controller, memory, finalizer, state


def test_finalize_proves_original_session_and_commits_terminal(tmp_path: Path) -> None:
    store, controller, _memory, finalizer, state = _conversation(tmp_path)
    terminal = asyncio.run(finalizer.finalize(state.run_id))
    assert terminal.task_state is TaskState.SUCCEEDED
    assert terminal.finalize_generation == 1

    operations = controller.operations(state.run_id)
    assert len(operations) == 4
    assert all(item.status is OperationStatus.COMPLETED for item in operations)
    for step in FinalizeStep:
        identity = FinalizeIdentity.for_step(terminal, 1, step)
        operation = next(item for item in operations if item.stable_key == identity.stable_key)
        attempt = controller.current_attempt(operation.operation_id)
        assert attempt.attempt_no == 1
        assert attempt.result and attempt.result_hash
        evidence = FinalizeEvidenceCodec.verify(attempt.result, attempt.result_hash)
        assert evidence.run_id == state.run_id
        assert evidence.generation == 1

    inbox = [item for item in store.list_inbox() if item.run_id == state.run_id]
    assert len(inbox) == 1
    events = controller.events(state.run_id)
    assert sum(event.type == "run_terminal" for event in events) == 1


def test_finalize_memory_evidence_can_span_indexed_segments(tmp_path: Path) -> None:
    _store, controller, _memory, finalizer, state = _conversation(
        tmp_path, split_segments=True,
    )
    terminal = asyncio.run(finalizer.finalize(state.run_id))
    identity = FinalizeIdentity.for_step(terminal, 1, FinalizeStep.MEMORY)
    operation = next(
        item for item in controller.operations(state.run_id)
        if item.stable_key == identity.stable_key
    )
    attempt = controller.current_attempt(operation.operation_id)
    evidence = FinalizeEvidenceCodec.verify(attempt.result or "", attempt.result_hash or "")
    assert isinstance(evidence, VerifiedArtifactEvidence)
    assert len(evidence.references.segments) == 2


def test_completed_memory_evidence_change_invalidates_generation(tmp_path: Path) -> None:
    _store, controller, memory, finalizer, state = _conversation(tmp_path)

    async def exercise() -> None:
        started = controller.apply(StartFinalizeGenerationCommand(
            command_id="generation-1", run_id=state.run_id,
            expected_revision=state.revision, gateway_epoch="epoch", generation=1,
        )).state
        await finalizer._ensure_file_step(started.run_id, 1, FinalizeStep.MEMORY)
        memory.record_assistant(
            started.session_id, "externally changed",
            audit={"run_id": started.run_id, "turn_id": started.turn_id},
        )
        recovered = await finalizer.finalize(started.run_id)
        assert recovered.task_state is TaskState.RECOVERY_REQUIRED
        assert controller.finalize_generation_invalidated(started.run_id, 1)
        memory_identity = FinalizeIdentity.for_step(recovered, 1, FinalizeStep.MEMORY)
        operation = next(
            item for item in controller.operations(started.run_id)
            if item.stable_key == memory_identity.stable_key
        )
        assert operation.status is OperationStatus.COMPLETED
        assert len(controller.operation_attempts(operation.operation_id)) == 1
        terminal = await finalizer.recover_generation(started.run_id, "artifact repaired")
        assert terminal.task_state is TaskState.SUCCEEDED
        assert terminal.finalize_generation == 2
        assert len(controller.operation_attempts(operation.operation_id)) == 1
        assert any(
            item.stable_key.startswith("finalize:v2:g2:")
            for item in controller.operations(started.run_id)
        )

    asyncio.run(exercise())


def test_terminal_rejects_missing_or_wrong_generation_evidence(tmp_path: Path) -> None:
    _store, controller, _memory, _finalizer, state = _conversation(tmp_path)
    state = controller.apply(StartFinalizeGenerationCommand(
        command_id="generation-1", run_id=state.run_id,
        expected_revision=state.revision, gateway_epoch="epoch", generation=1,
    )).state
    with pytest.raises(StateInvariantError):
        controller.apply(FinalizeTerminalCommand(
            command_id="terminal-without-evidence", run_id=state.run_id,
            expected_revision=state.revision, gateway_epoch="epoch", generation=1,
        ))


def test_session_append_once_reuses_timestamp_but_rejects_content_conflict(tmp_path: Path) -> None:
    memory = MemoryStore(
        tmp_path / ".yy" / "memory", workspace_root=tmp_path, agent_root=tmp_path,
    )
    session_id = memory.create_session("task", session_id="b" * 16)
    record = {
        "role": "assistant", "content": "same", "record_id": "stable-record",
        "timestamp": "2026-01-01 00:00:00", "run_id": "run", "turn_id": "turn",
    }
    assert memory.sessions.append_once(session_id, record)
    retry = {**record, "timestamp": "2026-01-02 00:00:00"}
    assert not memory.sessions.append_once(session_id, retry)
    with pytest.raises(ValueError, match="content conflict"):
        memory.sessions.append_once(session_id, {**retry, "content": "different"})


def test_session_reservation_is_owner_reentrant_and_session_scoped() -> None:
    async def exercise() -> None:
        registry = SessionReservationRegistry()
        await registry.acquire("project", "session-a", owner_id="run-a", wait=False)
        await registry.acquire("project", "session-a", owner_id="run-a", wait=False)
        with pytest.raises(SessionReservationConflict):
            await registry.acquire("project", "session-a", owner_id="run-b", wait=False)
        await registry.acquire("project", "session-b", owner_id="run-b", wait=False)
        assert await registry.is_reserved("project", "session-a")
        assert await registry.is_reserved("project", "session-b")
        await registry.release_owner("run-a")
        assert not await registry.is_reserved("project", "session-a")
        assert await registry.is_reserved("project", "session-b")

    asyncio.run(exercise())


def test_evidence_codec_is_deterministic_and_has_no_self_hash() -> None:
    evidence = VerifiedArtifactEvidence(
        step=FinalizeStep.MEMORY,
        run_id="run",
        generation=1,
        operation_id="operation",
        attempt_id="attempt",
        artifact_kind="session_record_chain",
        artifact_id="session:run:turn",
        artifact_hash="0" * 64,
        verification_method="strict_original_session_records",
    )
    first = FinalizeEvidenceCodec.encode(evidence)
    second = FinalizeEvidenceCodec.encode(evidence)
    assert first.serialized == second.serialized
    assert first.result_hash == second.result_hash
    assert "evidence_hash" not in first.serialized
