"""仅供 crash-injection 测试启动并强杀的独立进程。"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Agent.state import (
    BeginOperationCommand,
    ExecutionState,
    OperationKind,
    StartOperationCommand,
    TaskState,
    ToolIdempotency,
    TransitionCommand,
    WorkloadKind,
)
from gateway.state_controller import StateController
from gateway.store import GatewayStore


root = Path(sys.argv[1]).resolve()
ready = Path(sys.argv[2]).resolve()
store = GatewayStore(root / ".yy" / "gateway")
project = store.register_project(root)
controller = StateController(store.database_path, gateway_epoch="crashed-gateway")
run_id = uuid4().hex
state, _ = controller.create_run(
    run_id=run_id,
    workload_kind=WorkloadKind.CHAT,
    project_id=project.project_id,
    client_id="crash-test",
    task="external side effect",
    idempotency_key=uuid4().hex,
    request_hash=hashlib.sha256(b"external side effect").hexdigest(),
)
for target in (TaskState.QUEUED, TaskState.STARTING):
    state = controller.apply(TransitionCommand(
        command_id=uuid4().hex, run_id=run_id, expected_revision=state.revision,
        gateway_epoch="crashed-gateway", task_state=target, reason="test",
    )).state
state = controller.apply(TransitionCommand(
    command_id=uuid4().hex, run_id=run_id, expected_revision=state.revision,
    gateway_epoch="crashed-gateway", task_state=TaskState.RUNNING,
    execution_state=ExecutionState.THINKING, reason="test",
)).state
operation_id = uuid4().hex
state = controller.apply(BeginOperationCommand(
    command_id=uuid4().hex, run_id=run_id, expected_revision=state.revision,
    gateway_epoch="crashed-gateway", operation_id=operation_id,
    kind=OperationKind.TOOL, name="external_write",
    request_hash=hashlib.sha256(b"write-once").hexdigest(),
    idempotency=ToolIdempotency.NON_IDEMPOTENT,
)).state
state = controller.apply(TransitionCommand(
    command_id=uuid4().hex, run_id=run_id, expected_revision=state.revision,
    gateway_epoch="crashed-gateway", execution_state=ExecutionState.ACTING, reason="dispatch",
)).state
controller.apply(StartOperationCommand(
    command_id=uuid4().hex, run_id=run_id, expected_revision=state.revision,
    gateway_epoch="crashed-gateway", operation_id=operation_id,
))

# 模拟外部副作用已经成功，但 CompleteOperationCommand 尚未提交。
(root / "external-effect.txt").write_text("applied-once", encoding="utf-8")
ready.write_text(json.dumps({"run_id": run_id, "operation_id": operation_id}), encoding="utf-8")
while True:
    time.sleep(1)
