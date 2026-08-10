from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from Agent.state import (
    AgentState,
    BeginOperationCommand,
    CompleteOperationCommand,
    CreateApprovalCommand,
    DecideApprovalCommand,
    DurableApproval,
    ExecutionOutcome,
    ExecutionState,
    FinalizeTerminalCommand,
    INNER_TRANSITIONS,
    MarkOperationUnknownCommand,
    OperationKind,
    OperationStatus,
    OUTER_TRANSITIONS,
    StartOperationCommand,
    TaskState,
    TerminalTarget,
    ToolIdempotency,
    TransitionCommand,
    UpdateStateMetadataCommand,
    WorkloadKind,
    is_runnable,
    validate_inner_transition,
    validate_outer_transition,
)
from gateway.events import GatewayEventBus
from gateway.finalize import FinalizeCoordinator
from gateway.session_reservation import SessionReservationRegistry
from gateway.outbox import OutboxDispatcher
from gateway.recovery import RecoveryCoordinator
from gateway.state_controller import StateConflictError, StateController
from gateway.store import GatewayStore


class DurableRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = GatewayStore(self.root / ".yy" / "gateway")
        self.project = self.store.register_project(self.root)
        self.controller = StateController(self.store.database_path, gateway_epoch="epoch-a")
        request_hash = hashlib.sha256(b"task").hexdigest()
        self.state, duplicate = self.controller.create_run(
            run_id=uuid4().hex,
            workload_kind=WorkloadKind.CHAT,
            project_id=self.project.project_id,
            client_id="client",
            task="task",
            idempotency_key=uuid4().hex,
            request_hash=request_hash,
        )
        self.assertFalse(duplicate)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def transition(self, **changes):
        state = self.controller.state(self.state.run_id)
        result = self.controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch="epoch-a",
            reason="test",
            **changes,
        ))
        self.state = result.state
        return result.state

    def start_agent(self) -> None:
        self.transition(task_state=TaskState.QUEUED)
        self.transition(task_state=TaskState.STARTING)
        self.transition(task_state=TaskState.RUNNING, execution_state=ExecutionState.THINKING)

    def test_transition_tables_accept_every_declared_edge_and_reject_others(self) -> None:
        for source, targets in OUTER_TRANSITIONS.items():
            for target in TaskState:
                if target in targets:
                    validate_outer_transition(source, target)
                else:
                    with self.assertRaises(ValueError):
                        validate_outer_transition(source, target)
        for source, targets in INNER_TRANSITIONS.items():
            for target in ExecutionState:
                if target in targets:
                    validate_inner_transition(source, target)
                else:
                    with self.assertRaises(ValueError):
                        validate_inner_transition(source, target)
        for terminal in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED}:
            self.assertEqual(OUTER_TRANSITIONS[terminal], frozenset())
        self.assertEqual(INNER_TRANSITIONS[ExecutionState.FINISHED], frozenset())

    def test_finished_requires_outcome_and_all_normal_paths_use_finalizing(self) -> None:
        self.start_agent()
        with self.assertRaises(ValueError):
            TransitionCommand(
                command_id="bad", run_id=self.state.run_id,
                expected_revision=self.state.revision, gateway_epoch="epoch-a",
                execution_state=ExecutionState.FINISHED, reason="bad",
            )
        self.transition(
            execution_state=ExecutionState.FINISHED,
            outcome=ExecutionOutcome.SUCCESS,
            finish_reason="done",
        )
        self.transition(task_state=TaskState.FINALIZING, terminal_target=TerminalTarget.SUCCEEDED)
        state = self.controller.state(self.state.run_id)
        with self.assertRaises(Exception):
            FinalizeTerminalCommand(
                command_id=f"finalize:{state.run_id}:terminal", run_id=state.run_id,
                expected_revision=state.revision, gateway_epoch="epoch-a",
            )
        self.assertEqual(state.task_state, TaskState.FINALIZING)
        with self.assertRaises(ValueError):
            validate_outer_transition(TaskState.SUCCEEDED, TaskState.RECOVERING)

    def test_update_metadata_cannot_smuggle_lifecycle_fields(self) -> None:
        with self.assertRaises(Exception):
            UpdateStateMetadataCommand.model_validate({
                "command_id": "bad",
                "run_id": self.state.run_id,
                "expected_revision": 0,
                "gateway_epoch": "epoch-a",
                "task_state": "running",
            })

    def test_command_idempotency_and_revision_cas(self) -> None:
        command = TransitionCommand(
            command_id="stable-command",
            run_id=self.state.run_id,
            expected_revision=0,
            gateway_epoch="epoch-a",
            task_state=TaskState.QUEUED,
            reason="queue",
        )
        first = self.controller.apply(command)
        repeated = self.controller.apply(command)
        self.assertTrue(repeated.duplicate)
        self.assertEqual(first.state.revision, repeated.state.revision)
        with self.assertRaises(StateConflictError):
            self.controller.apply(TransitionCommand(
                command_id="stale",
                run_id=self.state.run_id,
                expected_revision=0,
                gateway_epoch="epoch-a",
                task_state=TaskState.STARTING,
                reason="stale",
            ))

    def test_tool_operation_uses_prepared_running_completed_boundary(self) -> None:
        self.start_agent()
        operation_id = uuid4().hex
        state = self.controller.state(self.state.run_id)
        prepared = self.controller.apply(BeginOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch="epoch-a",
            operation_id=operation_id,
            turn_id="turn-1",
            kind=OperationKind.TOOL,
            name="write",
            request_hash=hashlib.sha256(b"request").hexdigest(),
            idempotency=ToolIdempotency.IDEMPOTENT,
        ))
        self.assertEqual(prepared.operation.status, OperationStatus.PREPARED)
        state = prepared.state
        started = self.controller.apply(StartOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch="epoch-a",
            operation_id=operation_id,
            heartbeat_expires_at=(datetime.now().astimezone() + timedelta(minutes=1)).isoformat(),
        ))
        self.assertEqual(started.operation.status, OperationStatus.RUNNING)
        state = started.state
        completed = self.controller.apply(CompleteOperationCommand(
            command_id=uuid4().hex,
            run_id=state.run_id,
            expected_revision=state.revision,
            gateway_epoch="epoch-a",
            operation_id=operation_id,
            result="ok",
            result_hash=hashlib.sha256(b"ok").hexdigest(),
            result_source="test",
        ))
        self.assertEqual(completed.operation.status, OperationStatus.COMPLETED)

    def test_unknown_side_effect_is_not_runnable(self) -> None:
        self.start_agent()
        operation_id = uuid4().hex
        state = self.controller.state(self.state.run_id)
        result = self.controller.apply(BeginOperationCommand(
            command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
            gateway_epoch="epoch-a", operation_id=operation_id, kind=OperationKind.TOOL,
            name="external", request_hash=hashlib.sha256(b"x").hexdigest(),
            idempotency=ToolIdempotency.NON_IDEMPOTENT,
        ))
        self.state = result.state
        self.transition(execution_state=ExecutionState.ACTING)
        state = self.controller.state(self.state.run_id)
        started = self.controller.apply(StartOperationCommand(
            command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
            gateway_epoch="epoch-a", operation_id=operation_id,
        ))
        state = started.state
        unknown = self.controller.apply(MarkOperationUnknownCommand(
            command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
            gateway_epoch="epoch-a", operation_id=operation_id, unknown_reason="crash",
        ))
        operation = unknown.operation
        self.assertFalse(is_runnable(unknown.state, operation, now=datetime.now().astimezone()))
        self.assertEqual(unknown.state.execution.state, ExecutionState.ACTING)

    def test_durable_approval_repeated_decision_and_conflict(self) -> None:
        state = self.controller.state(self.state.run_id)
        prepared = self.controller.apply(BeginOperationCommand(
            command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
            gateway_epoch="epoch-a", operation_id=uuid4().hex,
            kind=OperationKind.TOOL, name="write",
            request_hash=hashlib.sha256(b"{}").hexdigest(),
            idempotency=ToolIdempotency.IDEMPOTENT,
        ))
        attempt = self.controller.current_attempt(prepared.operation.operation_id)
        approval = DurableApproval(
            approval_id=uuid4().hex,
            operation_id=prepared.operation.operation_id,
            attempt_id=attempt.attempt_id,
            attempt_no=attempt.attempt_no,
            stable_key=f"approval:{prepared.operation.stable_key}:{attempt.attempt_no}",
            request_hash=prepared.operation.request_hash,
            run_id=self.state.run_id,
            client_id="client",
            tool_name="write",
            arguments_hash=hashlib.sha256(b"{}").hexdigest(),
            arguments_json="{}",
            created_at=datetime.now().astimezone().isoformat(),
            expires_at=(datetime.now().astimezone() + timedelta(minutes=10)).isoformat(),
        )
        state = self.controller.state(self.state.run_id)
        created = self.controller.apply(CreateApprovalCommand(
            command_id=uuid4().hex, run_id=state.run_id, expected_revision=state.revision,
            gateway_epoch="epoch-a", approval=approval,
        ))
        state = created.state
        command = DecideApprovalCommand(
            command_id=f"approval:{approval.approval_id}:approve",
            run_id=state.run_id, expected_revision=state.revision, gateway_epoch="epoch-a",
            approval_id=approval.approval_id, approved=True, decided_by="client",
        )
        first = self.controller.apply(command)
        repeated = self.controller.apply(command)
        self.assertTrue(repeated.duplicate)
        state = self.controller.state(self.state.run_id)
        with self.assertRaises(StateConflictError):
            self.controller.apply(DecideApprovalCommand(
                command_id=f"approval:{approval.approval_id}:deny",
                run_id=state.run_id, expected_revision=state.revision, gateway_epoch="epoch-a",
                approval_id=approval.approval_id, approved=False, decided_by="client",
            ))

    def test_outbox_confirms_both_sinks_independently(self) -> None:
        async def check() -> None:
            bus = GatewayEventBus()
            dispatcher = OutboxDispatcher(
                self.store.database_path,
                self.store.runs_directory,
                bus.publish,
            )
            await dispatcher.drain_once()
            with self.store._connect() as connection:
                rows = connection.execute("SELECT * FROM event_outbox").fetchall()
            self.assertTrue(rows)
            self.assertTrue(all(row["eventbus_status"] == "sent" for row in rows))
            self.assertTrue(all(row["jsonl_status"] == "sent" for row in rows))
            self.assertTrue(all(row["delivered_at"] for row in rows))

        asyncio.run(check())

    def test_outbox_recovers_each_death_window_without_rewriting_other_sink(self) -> None:
        async def check_jsonl_failure() -> None:
            published: list[str] = []

            async def publish(event) -> None:
                published.append(event.event_id)

            dispatcher = OutboxDispatcher(
                self.store.database_path, self.store.runs_directory, publish,
                retry_base_seconds=0, retry_max_seconds=0,
            )
            original = dispatcher.jsonl.append_once

            def fail_jsonl(event):
                raise OSError("injected jsonl failure")

            dispatcher.jsonl.append_once = fail_jsonl
            await dispatcher.drain_once()
            with self.store._connect() as connection:
                first = connection.execute("SELECT * FROM event_outbox ORDER BY sequence LIMIT 1").fetchone()
            self.assertEqual(first["eventbus_status"], "sent")
            self.assertEqual(first["jsonl_status"], "failed")
            self.assertIsNone(first["delivered_at"])
            dispatcher.jsonl.append_once = original
            await dispatcher.drain_once()
            with self.store._connect() as connection:
                final = connection.execute("SELECT * FROM event_outbox ORDER BY sequence LIMIT 1").fetchone()
            self.assertEqual(final["eventbus_attempts"], 1)
            self.assertEqual(final["jsonl_status"], "sent")
            self.assertIsNotNone(final["delivered_at"])
            self.assertEqual(len(published), 1)

        asyncio.run(check_jsonl_failure())

    def test_real_process_kill_preserves_unknown_side_effect(self) -> None:
        crash_root = self.root / "killspace"
        crash_root.mkdir()
        ready = crash_root / "crash-ready.json"
        worker = Path(__file__).with_name("_durable_crash_worker.py")
        process = subprocess.Popen(
            [sys.executable, str(worker), str(crash_root), str(ready)],
            cwd=Path(__file__).resolve().parents[1],
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "crash worker 未进入副作用死亡窗口")
            metadata = json.loads(ready.read_text(encoding="utf-8"))
            process.kill()  # Windows=TerminateProcess，POSIX=SIGKILL
            process.wait(timeout=5)

            restarted_store = GatewayStore(crash_root / ".yy" / "gateway")
            restarted = StateController(restarted_store.database_path, gateway_epoch="restarted")
            queued: list[object] = []

            async def enqueue(run) -> None:
                queued.append(run)

            coordinator = RecoveryCoordinator(restarted, restarted_store, enqueue)
            counts = asyncio.run(coordinator.recover())
            state = restarted.state(metadata["run_id"])
            operation = restarted.operation(metadata["operation_id"])
            self.assertEqual(state.task_state, TaskState.RECOVERY_REQUIRED)
            self.assertEqual(state.execution.state, ExecutionState.ACTING)
            self.assertEqual(operation.status, OperationStatus.UNKNOWN)
            self.assertEqual((crash_root / "external-effect.txt").read_text(encoding="utf-8"), "applied-once")
            self.assertEqual(queued, [])
            self.assertEqual(counts["recovery_required"], 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_control_only_finalizing_creates_one_deterministic_inbox(self) -> None:
        state, _ = self.controller.create_run(
            run_id=uuid4().hex, workload_kind=WorkloadKind.MAINTENANCE,
            project_id=self.project.project_id, client_id="maintenance",
            task="maintenance", idempotency_key=uuid4().hex,
            request_hash=hashlib.sha256(b"maintenance").hexdigest(),
        )
        state = self.controller.apply(TransitionCommand(
            command_id=uuid4().hex, run_id=state.run_id,
            expected_revision=state.revision, gateway_epoch="epoch-a",
            task_state=TaskState.FINALIZING,
            terminal_target=TerminalTarget.SUCCEEDED, reason="done",
        )).state
        finalizer = FinalizeCoordinator(
            controller=self.controller, store=self.store, agent_root=self.root,
            reservations=SessionReservationRegistry(),
        )
        finished = asyncio.run(finalizer.finalize(state.run_id))
        self.assertEqual(finished.task_state, TaskState.SUCCEEDED)
        self.assertEqual(
            len([item for item in self.store.list_inbox() if item.run_id == state.run_id]), 1,
        )


if __name__ == "__main__":
    unittest.main()
