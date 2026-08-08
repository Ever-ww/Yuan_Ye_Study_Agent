from __future__ import annotations

import ast
import hashlib
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from Agent.state import (
    AttemptRecoveryResolution,
    CompleteFinalizeStepCommand,
    CreateOperationWithAttemptCommand,
    FinalizeTerminalCommand,
    ExecutionState,
    ImmutableOperationMetadata,
    OperationAttempt,
    OperationFailureKind,
    OperationKind,
    OperationStatus,
    RecoveryDecisionCommand,
    ReconcileOperationAttemptCommand,
    ReconcileResult,
    ReconcileStatus,
    RetryPolicySnapshot,
    TaskState,
    TerminalTarget,
    ToolIdempotency,
    TransitionCommand,
    WorkloadKind,
    reduce_operation,
)
from gateway.state_controller import StateConflictError, StateController
from gateway.outbox import OutboxDispatcher
from gateway.durable_execution import DurableToolCoordinator
from gateway.store import GatewayStore
from Agent import load_runtime_config
from gateway.application import GatewayApplication
from gateway.api import create_gateway_api
from fastapi.testclient import TestClient


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def durable(tmp_path: Path):
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    project = store.register_project(tmp_path)
    controller = StateController(store.database_path, gateway_epoch="epoch")
    state, _ = controller.create_run(
        run_id=uuid4().hex, workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id, client_id="client", task="task",
        idempotency_key=uuid4().hex, request_hash=_hash("task"),
    )
    return controller, state


def _create(controller: StateController, state, *, stable_key="tool:t:m:c", request_hash=None):
    selected_hash = request_hash or _hash("request")
    return controller.apply(CreateOperationWithAttemptCommand(
        command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
        gateway_epoch="epoch", operation_id=uuid4().hex, attempt_id=uuid4().hex,
        turn_id="turn", kind=OperationKind.TOOL, name="write",
        stable_key=stable_key, request_hash=selected_hash,
        idempotency=ToolIdempotency.NON_IDEMPOTENT, side_effecting=True,
        source_model_call_id="m", tool_call_id="c",
        retry_policy_snapshot=RetryPolicySnapshot(
            max_attempts=3, base_seconds=0, max_seconds=0, automatic=False,
            requires_reconcile=True, requires_human_confirmation=True,
        ),
    ))


def test_operation_and_attempt_one_are_created_atomically(durable) -> None:
    controller, state = durable
    result = _create(controller, state)
    assert result.operation is not None and result.attempt is not None
    assert result.operation.latest_attempt_no == 1
    assert controller.operation_attempts(result.operation.operation_id) == (result.attempt,)
    with controller._connection() as connection:  # invariant check at storage boundary
        missing = connection.execute(
            "SELECT operation_id FROM operation_ledger WHERE operation_id NOT IN "
            "(SELECT DISTINCT operation_id FROM operation_attempts)",
        ).fetchall()
    assert missing == []


def test_same_stable_key_with_different_request_hash_is_rejected(durable) -> None:
    controller, state = durable
    first = _create(controller, state)
    with pytest.raises(StateConflictError):
        _create(
            controller, first.state, stable_key=first.operation.stable_key,
            request_hash=_hash("different"),
        )
    assert len(controller.operation_attempts(first.operation.operation_id)) == 1


def test_reducer_exhausts_policy_without_rewriting_attempt_evidence() -> None:
    policy = RetryPolicySnapshot(
        max_attempts=2, base_seconds=2, max_seconds=60, automatic=True,
        requires_reconcile=False, requires_human_confirmation=False,
    )
    metadata = ImmutableOperationMetadata(
        operation_id="op", run_id="run", kind=OperationKind.MODEL, name="model",
        stable_key="model:t:1", request_hash=_hash("request"), side_effecting=False,
    )
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    attempts = tuple(
        OperationAttempt(
            attempt_id=f"a{number}", operation_id="op", run_id="run",
            attempt_no=number, request_hash=metadata.request_hash, side_effecting=False,
            status=OperationStatus.FAILED,
            failure_kind=OperationFailureKind.RETRYABLE,
            failure_reason="network", completed_at=(base + timedelta(seconds=number)).isoformat(),
            created_at=base.isoformat(), updated_at=base.isoformat(),
        )
        for number in (1, 2)
    )
    aggregate = reduce_operation(metadata, attempts, policy)
    assert aggregate.status is OperationStatus.FAILED
    assert aggregate.failure_kind is OperationFailureKind.TERMINAL
    assert aggregate.failure_reason == "retry_exhausted"
    assert attempts[-1].failure_kind is OperationFailureKind.RETRYABLE


def test_unknown_manual_retry_preserves_original_attempt(durable) -> None:
    controller, state = durable
    created = _create(controller, state)
    from Agent.state import MarkOperationAttemptUnknownCommand, StartOperationAttemptCommand

    started = controller.apply(StartOperationAttemptCommand(
        command_id=uuid4().hex, run_id=state.run_id,
        expected_revision=created.state.revision, gateway_epoch="epoch",
        attempt_id=created.attempt.attempt_id,
    ))
    unknown = controller.apply(MarkOperationAttemptUnknownCommand(
        command_id=uuid4().hex, run_id=state.run_id,
        expected_revision=started.state.revision, gateway_epoch="epoch",
        attempt_id=created.attempt.attempt_id, failure_reason="crash window",
    ))
    retried = controller.apply(RecoveryDecisionCommand(
        command_id=uuid4().hex, run_id=state.run_id,
        expected_revision=unknown.state.revision, gateway_epoch="epoch",
        action="retry", operation_id=created.operation.operation_id,
        actor="user", reason="confirmed external state", risk_confirmed=True,
    ))
    attempts = controller.operation_attempts(created.operation.operation_id)
    assert len(attempts) == 2
    assert attempts[0].status is OperationStatus.UNKNOWN
    assert attempts[0].failure_kind is OperationFailureKind.UNKNOWN_EFFECT
    assert attempts[0].recovery_resolution is AttemptRecoveryResolution.RETRY_AUTHORIZED
    assert attempts[1].status is OperationStatus.PREPARED
    assert retried.attempt == attempts[1]


def test_reconcile_success_resolves_unknown_without_rewriting_raw_status(durable) -> None:
    controller, state = durable
    created = _create(controller, state)
    from Agent.state import MarkOperationAttemptUnknownCommand, StartOperationAttemptCommand

    started = controller.apply(StartOperationAttemptCommand(
        command_id=uuid4().hex, run_id=state.run_id,
        expected_revision=created.state.revision, gateway_epoch="epoch",
        attempt_id=created.attempt.attempt_id,
    ))
    unknown = controller.apply(MarkOperationAttemptUnknownCommand(
        command_id=uuid4().hex, run_id=state.run_id,
        expected_revision=started.state.revision, gateway_epoch="epoch",
        attempt_id=created.attempt.attempt_id, failure_reason="lost response",
    ))
    resolved = controller.apply(ReconcileOperationAttemptCommand(
        command_id=uuid4().hex, run_id=state.run_id,
        expected_revision=unknown.state.revision, gateway_epoch="epoch",
        attempt_id=created.attempt.attempt_id,
        result=ReconcileResult(
            status=ReconcileStatus.COMPLETED, evidence="external record", result_source="api",
            observed_result="done", checked_at=datetime.now(timezone.utc).isoformat(),
        ),
    ))
    assert resolved.attempt.status is OperationStatus.UNKNOWN
    assert resolved.attempt.failure_kind is OperationFailureKind.UNKNOWN_EFFECT
    assert resolved.attempt.recovery_resolution is AttemptRecoveryResolution.CONFIRMED_SUCCEEDED
    assert resolved.operation.status is OperationStatus.COMPLETED


def test_finalize_terminal_requires_all_attempt_backed_steps(durable) -> None:
    controller, state = durable
    state = controller.apply(TransitionCommand(
        command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
        gateway_epoch="epoch", task_state=TaskState.FINALIZING,
        terminal_target=TerminalTarget.SUCCEEDED, reason="finish",
    )).state
    with pytest.raises(Exception):
        controller.apply(FinalizeTerminalCommand(
            command_id="terminal-early", run_id=state.run_id,
            expected_revision=state.revision, gateway_epoch="epoch",
        ))
    assert controller.state(state.run_id).task_state is TaskState.FINALIZING
    assert not any(event.type == "run_terminal" for event in controller.events(state.run_id))
    for step in ("memory", "session_index", "audit", "inbox"):
        state = controller.state(state.run_id)
        state = controller.apply(CompleteFinalizeStepCommand(
            command_id=f"finalize:{state.run_id}:{step}", run_id=state.run_id,
            expected_revision=state.revision, gateway_epoch="epoch",
            step_name=step, result="completed",
        )).state
    result = controller.apply(FinalizeTerminalCommand(
        command_id="terminal", run_id=state.run_id,
        expected_revision=state.revision, gateway_epoch="epoch",
    ))
    assert result.state.task_state is TaskState.SUCCEEDED
    events = controller.events(state.run_id)
    assert events[-1].type == "run_terminal"


def test_client_idempotency_scope_is_composite(durable) -> None:
    controller, first = durable
    request_hash = _hash("same")
    second, duplicate = controller.create_run(
        run_id=uuid4().hex, workload_kind=WorkloadKind.CHAT,
        project_id=first.project_id, client_id="other-client", task="same",
        idempotency_key=first.idempotency_key, request_hash=request_hash,
    )
    assert not duplicate
    repeated, duplicate = controller.create_run(
        run_id=uuid4().hex, workload_kind=WorkloadKind.CHAT,
        project_id=first.project_id, client_id="other-client", task="same",
        idempotency_key=first.idempotency_key, request_hash=request_hash,
    )
    assert duplicate and repeated.run_id == second.run_id


def test_outbox_dead_letters_only_the_failing_sink(durable, tmp_path: Path) -> None:
    controller, _ = durable

    async def fail(_event) -> None:
        raise OSError("event bus unavailable")

    dispatcher = OutboxDispatcher(
        controller.database_path, tmp_path / "events", fail,
        retry_max_attempts=2, retry_base_seconds=0, retry_max_seconds=0,
        dead_letter_enabled=True,
    )
    asyncio.run(dispatcher.drain_once())
    asyncio.run(dispatcher.drain_once())
    with controller._connection() as connection:
        row = connection.execute(
            "SELECT * FROM event_outbox ORDER BY sequence LIMIT 1",
        ).fetchone()
    assert row["eventbus_dead_letter_at"] is not None
    assert row["jsonl_status"] == "sent"
    assert row["delivered_at"] is None


def test_retryable_tool_uses_new_attempts_and_executes_until_success(durable, tmp_path: Path) -> None:
    controller, state = durable
    for target in (TaskState.QUEUED, TaskState.STARTING):
        state = controller.apply(TransitionCommand(
            command_id=uuid4().hex, run_id=state.run_id,
            expected_revision=state.revision, gateway_epoch="epoch",
            task_state=target, reason="test",
        )).state
    state = controller.apply(TransitionCommand(
        command_id=uuid4().hex, run_id=state.run_id,
        expected_revision=state.revision, gateway_epoch="epoch",
        task_state=TaskState.RUNNING,
        execution_state=ExecutionState.THINKING,
        reason="test",
    )).state
    coordinator = DurableToolCoordinator(
        controller, retry_max_attempts=3, retry_base_seconds=0, retry_max_seconds=0,
    )
    token = coordinator.bind(state.run_id, "turn")
    calls = 0

    class PureTool:
        idempotency = ToolIdempotency.PURE

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("temporary")
        return "ok"

    async def run() -> None:
        operation = await coordinator.prepare(
            tool=PureTool(), name="read", arguments={"path": "x"}, risk="read",
            context=SimpleNamespace(workspace_root=tmp_path), tool_call_id="call",
        )
        assert await coordinator.execute(operation, invoke) == "ok"
        attempts = controller.operation_attempts(operation.operation_id)
        assert [item.status for item in attempts] == [
            OperationStatus.FAILED, OperationStatus.FAILED, OperationStatus.COMPLETED,
        ]
        assert all(
            item.failure_kind is OperationFailureKind.RETRYABLE for item in attempts[:2]
        )
        assert controller.operation(operation.operation_id).status is OperationStatus.COMPLETED

    try:
        asyncio.run(run())
    finally:
        coordinator.reset(token)
    assert calls == 3


def test_idempotency_header_body_conflict_returns_400(tmp_path: Path) -> None:
    application = GatewayApplication(load_runtime_config(tmp_path))
    headers = {"Authorization": "Bearer token", "Idempotency-Key": "header-key"}
    with TestClient(create_gateway_api(application, access_token="token")) as client:
        project = client.post(
            "/api/v1/projects", headers={"Authorization": "Bearer token"},
            json={"path": str(tmp_path)},
        ).json()
        response = client.post(
            "/api/v1/runs", headers=headers,
            json={
                "project_id": project["project_id"], "client_id": "client", "task": "task",
                "idempotency_key": "body-key",
            },
        )
    assert response.status_code == 400


def test_production_runtime_cannot_bypass_state_controller() -> None:
    """生命周期状态只能由 StateController Command 修改。"""
    root = Path(__file__).resolve().parents[1]
    production_roots = ("Agent", "gateway", "dream", "context_process", "tool")
    normal_terminals = {"SUCCEEDED", "FAILED", "CANCELLED"}
    lifecycle_fields = {
        "task_state", "execution_state", "outcome", "finish_reason",
        "terminal_target", "recovery_required",
    }
    violations: list[str] = []
    for package in production_roots:
        for path in (root / package).rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = (
                        node.func.attr if isinstance(node.func, ast.Attribute)
                        else node.func.id if isinstance(node.func, ast.Name)
                        else ""
                    )
                    if name == "update_run":
                        if any(keyword.arg == "status" for keyword in node.keywords):
                            violations.append(f"{relative}:{node.lineno}: update_run(status=...)")
                    if name == "TransitionCommand":
                        for keyword in node.keywords:
                            value = keyword.value
                            if (
                                keyword.arg == "task_state"
                                and isinstance(value, ast.Attribute)
                                and value.attr in normal_terminals
                            ):
                                violations.append(
                                    f"{relative}:{node.lineno}: direct normal terminal transition",
                                )
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute) and target.attr in lifecycle_fields:
                            violations.append(
                                f"{relative}:{node.lineno}: direct lifecycle assignment {target.attr}",
                            )
    assert violations == []
