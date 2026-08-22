from __future__ import annotations

import asyncio
import json
import tempfile
import time
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from Agent import AgentRuntime, load_runtime_config
from Agent.contracts import EventType, ModelReply, ToolCall
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.react.loop import ReactLoop
from Agent.state import (
    ExecutionState,
    MaterializedToolObservation,
    OperationKind,
    TaskState,
    TransitionCommand,
    WorkloadKind,
)
from gateway.durable_execution import DurableModelHooks, DurableToolCoordinator
from gateway.state_controller import StateConflictError, StateController
from gateway.store import GatewayStore
from memory import MemoryStore
from memory.callbacks import register_memory_callbacks
from tool import AsyncToolRegistry, ToolContext


class _ParallelRead:
    risk = "read"
    idempotency = "PURE"
    parallel_safe = True
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "delay": {"type": "number"},
            "fail": {"type": "boolean"},
        },
        "required": ["value", "delay"],
    }

    def __init__(self, name: str, tracker: dict[str, object]) -> None:
        self.name = name
        self.description = name
        self.tracker = tracker

    async def run(self, arguments, context):
        del context
        self.tracker["active"] = int(self.tracker.get("active", 0)) + 1
        self.tracker["peak"] = max(
            int(self.tracker.get("peak", 0)), int(self.tracker["active"]),
        )
        self.tracker.setdefault("started", []).append(arguments["value"])
        try:
            await asyncio.sleep(arguments["delay"])
            if arguments.get("fail"):
                raise RuntimeError(f"failed-{arguments['value']}")
            self.tracker.setdefault("finished", []).append(arguments["value"])
            return arguments["value"]
        except asyncio.CancelledError:
            self.tracker.setdefault("cancelled", []).append(arguments["value"])
            raise
        finally:
            self.tracker["active"] = int(self.tracker["active"]) - 1


class _SerialWrite:
    name = "serial_write"
    description = "serial barrier"
    risk = "write"
    idempotency = "IDEMPOTENT"
    parallel_safe = True
    schema = {"type": "object", "properties": {}}

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def run(self, arguments, context):
        del arguments, context
        self.order.append("write")
        return "write"


class _EndsTurn:
    name = "finish_task"
    description = "finish"
    risk = "read"
    idempotency = "PURE"
    parallel_safe = True
    schema = {"type": "object", "properties": {}}

    async def run(self, arguments, context):
        del arguments, context
        return json.dumps({"restart_required": True})

    @staticmethod
    def ends_turn(result: str) -> bool:
        return bool(json.loads(result)["restart_required"])


class _OneResponseProvider:
    streaming = False

    def __init__(self, calls: tuple[ToolCall, ...]) -> None:
        self.calls = calls
        self.count = 0

    async def complete(self, messages, tools):
        del tools
        self.count += 1
        if not any(item.get("role") == "tool" for item in messages):
            return ModelReply(tool_calls=self.calls)
        return ModelReply(text="done")


def _run(provider, registry, hooks, root: Path, *, maximum: int = 4):
    loop = ReactLoop(
        provider, registry, hooks, max_steps=3, max_parallel_tool_calls=maximum,
    )
    messages = [{"role": "user", "content": "test"}]

    async def collect():
        return [event async for event in loop.run(
            messages,
            ToolContext(project_root=root, approval=lambda name, args: _approve()),
            task="test",
            session_id="session-parallel",
            model={"provider": "test", "name": "test"},
        )]

    async def _approve():
        return True

    return asyncio.run(collect()), messages


def test_parallel_reads_overlap_but_publish_in_call_order() -> None:
    tracker: dict[str, object] = {}
    after_order: list[str] = []
    hooks = HookRegistry()

    async def after(event: HookEvent) -> None:
        after_order.append(str(event.data["tool_call_id"]))

    hooks.register(HookPoint.TOOL_AFTER, after)
    registry = AsyncToolRegistry([_ParallelRead("read", tracker)])
    provider = _OneResponseProvider((
        ToolCall(name="read", id="a", arguments={"value": "A", "delay": 0.09}),
        ToolCall(name="read", id="b", arguments={"value": "B", "delay": 0.01}),
        ToolCall(name="read", id="c", arguments={"value": "C", "delay": 0.04}),
    ))
    with tempfile.TemporaryDirectory() as value:
        started = time.perf_counter()
        events, messages = _run(provider, registry, hooks, Path(value))
        elapsed = time.perf_counter() - started
    assert elapsed < 0.16
    assert tracker["peak"] == 3
    assert tracker["finished"] == ["B", "C", "A"]
    assert after_order == ["a", "b", "c"]
    assert [item["tool_call_id"] for item in messages if item.get("role") == "tool"] == [
        "a", "b", "c",
    ]
    assert any(event.type is EventType.TOOL_BATCH_COMPLETED for event in events)
    assert next(
        index for index, event in enumerate(events)
        if event.type is EventType.TOOL_BATCH_STARTED
    ) < next(
        index for index, event in enumerate(events)
        if event.type is EventType.TOOL_COMPLETED
    )


def test_parallel_read_failure_does_not_cancel_siblings() -> None:
    tracker: dict[str, object] = {}
    registry = AsyncToolRegistry([_ParallelRead("read", tracker)])
    provider = _OneResponseProvider((
        ToolCall(name="read", id="a", arguments={"value": "A", "delay": 0.04}),
        ToolCall(name="read", id="b", arguments={"value": "B", "delay": 0.01, "fail": True}),
        ToolCall(name="read", id="c", arguments={"value": "C", "delay": 0.03}),
    ))
    with tempfile.TemporaryDirectory() as value:
        _, messages = _run(provider, registry, HookRegistry(), Path(value))
    assert set(tracker["finished"]) == {"A", "C"}
    tool_messages = [item for item in messages if item.get("role") == "tool"]
    assert [item["tool_call_id"] for item in tool_messages] == ["a", "b", "c"]
    assert "failed-B" in tool_messages[1]["content"]


def test_serial_barrier_flushes_before_write_and_prepares_later_call_after_write() -> None:
    tracker: dict[str, object] = {}
    order: list[str] = []
    hooks = HookRegistry()

    async def before(event: HookEvent) -> None:
        order.append(f"prepare:{event.data['tool_call_id']}")

    async def after(event: HookEvent) -> None:
        order.append(f"publish:{event.data['tool_call_id']}")

    hooks.register(HookPoint.TOOL_BEFORE, before)
    hooks.register(HookPoint.TOOL_AFTER, after)
    registry = AsyncToolRegistry([
        _ParallelRead("read", tracker),
        _SerialWrite(order),
    ])
    provider = _OneResponseProvider((
        ToolCall(name="read", id="a", arguments={"value": "A", "delay": 0.01}),
        ToolCall(name="serial_write", id="w", arguments={}),
        ToolCall(name="read", id="c", arguments={"value": "C", "delay": 0.01}),
    ))
    with tempfile.TemporaryDirectory() as value:
        _run(provider, registry, hooks, Path(value))
    assert order.index("publish:a") < order.index("write")
    assert order.index("write") < order.index("prepare:c")


def test_ends_turn_never_prepares_following_calls() -> None:
    tracker: dict[str, object] = {}
    prepared: list[str] = []
    hooks = HookRegistry()

    async def before(event: HookEvent) -> None:
        prepared.append(str(event.data["tool_call_id"]))

    hooks.register(HookPoint.TOOL_BEFORE, before)
    registry = AsyncToolRegistry([_EndsTurn(), _ParallelRead("read", tracker)])
    provider = _OneResponseProvider((
        ToolCall(name="finish_task", id="finish", arguments={}),
        ToolCall(name="read", id="later", arguments={"value": "later", "delay": 0.01}),
    ))
    with tempfile.TemporaryDirectory() as value:
        events, messages = _run(provider, registry, hooks, Path(value))
    assert prepared == ["finish"]
    assert not tracker.get("started")
    assert any(event.type is EventType.GATEWAY_RESTART_REQUIRED for event in events)
    assert [item["tool_call_id"] for item in messages if item.get("role") == "tool"] == [
        "finish", "later",
    ]


def test_parallel_limit_one_preserves_serial_execution() -> None:
    tracker: dict[str, object] = {}
    registry = AsyncToolRegistry([_ParallelRead("read", tracker)])
    provider = _OneResponseProvider((
        ToolCall(name="read", id="a", arguments={"value": "A", "delay": 0.01}),
        ToolCall(name="read", id="b", arguments={"value": "B", "delay": 0.01}),
    ))
    with tempfile.TemporaryDirectory() as value:
        _run(provider, registry, HookRegistry(), Path(value), maximum=1)
    assert tracker["peak"] == 1


def test_parallel_group_is_chunked_at_configured_limit() -> None:
    tracker: dict[str, object] = {}
    registry = AsyncToolRegistry([_ParallelRead("read", tracker)])
    provider = _OneResponseProvider(tuple(
        ToolCall(
            name="read",
            id=f"call-{index}",
            arguments={"value": str(index), "delay": 0.01},
        )
        for index in range(6)
    ))
    with tempfile.TemporaryDirectory() as value:
        events, messages = _run(
            provider, registry, HookRegistry(), Path(value), maximum=4,
        )
    assert tracker["peak"] == 4
    assert sum(event.type is EventType.TOOL_BATCH_STARTED for event in events) == 2
    assert [item["tool_call_id"] for item in messages if item.get("role") == "tool"] == [
        f"call-{index}" for index in range(6)
    ]


def test_duplicate_provider_tool_call_ids_are_normalized_stably() -> None:
    def execute_once(root: Path) -> list[str]:
        tracker: dict[str, object] = {}
        provider = _OneResponseProvider((
            ToolCall(name="read", id="duplicate", arguments={"value": "A", "delay": 0.0}),
            ToolCall(name="read", id="duplicate", arguments={"value": "B", "delay": 0.0}),
        ))
        _, messages = _run(
            provider,
            AsyncToolRegistry([_ParallelRead("read", tracker)]),
            HookRegistry(),
            root,
        )
        return [
            str(item["tool_call_id"])
            for item in messages if item.get("role") == "tool"
        ]

    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        first_ids = execute_once(Path(first))
        second_ids = execute_once(Path(second))
    assert first_ids[0] == "duplicate"
    assert len(first_ids) == len(set(first_ids)) == 2
    assert second_ids == first_ids


def test_run_cancellation_cancels_and_settles_entire_parallel_group(
    tmp_path: Path,
) -> None:
    tracker: dict[str, object] = {}
    loop = ReactLoop(
        _OneResponseProvider((
            ToolCall(name="read", id="a", arguments={"value": "A", "delay": 10.0}),
            ToolCall(name="read", id="b", arguments={"value": "B", "delay": 10.0}),
        )),
        AsyncToolRegistry([_ParallelRead("read", tracker)]),
        HookRegistry(),
        max_steps=2,
        max_parallel_tool_calls=4,
    )

    async def cancel_group() -> None:
        messages = [{"role": "user", "content": "cancel"}]

        async def collect() -> None:
            async for _ in loop.run(
                messages,
                ToolContext(project_root=tmp_path),
                task="cancel",
                session_id="session-parallel",
                model={"provider": "test", "name": "test"},
            ):
                pass

        running = asyncio.create_task(collect())
        for _ in range(100):
            if tracker.get("peak") == 2:
                break
            await asyncio.sleep(0.005)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(cancel_group())
    assert set(tracker["cancelled"]) == {"A", "B"}
    assert tracker["active"] == 0


def test_durable_parallel_group_commits_distinct_attempts_and_ordered_observations(
    tmp_path: Path,
) -> None:
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    project = store.register_project(tmp_path)
    controller = StateController(store.database_path, gateway_epoch="parallel-epoch")
    run_id = uuid4().hex
    state, _ = controller.create_run(
        run_id=run_id,
        workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id,
        client_id="parallel-test",
        task="parallel",
        idempotency_key=uuid4().hex,
        request_hash=hashlib.sha256(b"parallel").hexdigest(),
    )
    for task_state, execution_state in (
        (TaskState.QUEUED, None),
        (TaskState.STARTING, None),
        (TaskState.RUNNING, ExecutionState.THINKING),
    ):
        state = controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch="parallel-epoch",
            task_state=task_state,
            execution_state=execution_state,
            reason="test setup",
        )).state

    tracker: dict[str, object] = {}
    provider = _OneResponseProvider((
        ToolCall(name="read", id="a", arguments={"value": "A", "delay": 0.04}),
        ToolCall(name="read", id="b", arguments={"value": "B", "delay": 0.01}),
    ))
    memory = MemoryStore(tmp_path / ".yy" / "memory")
    hooks = HookRegistry()
    register_memory_callbacks(hooks, memory)
    coordinator = DurableToolCoordinator(controller, heartbeat_seconds=60)
    DurableModelHooks(controller, memory).register(hooks)
    runtime = AgentRuntime(
        load_runtime_config(tmp_path),
        provider=provider,
        tools=AsyncToolRegistry([_ParallelRead("read", tracker)]),
        hooks=hooks,
        memory=memory,
        enable_sandbox=False,
        raise_errors=True,
    )
    runtime.tool_context = runtime.tool_context.model_copy(
        update={"operation_coordinator": coordinator},
    )
    async def run_bound():
        token = coordinator.bind(run_id)
        try:
            return await runtime.run("parallel")
        finally:
            coordinator.reset(token)

    result = asyncio.run(run_bound())
    assert result.completed
    assert provider.count == 2, (provider.count, result.answer, tracker)
    diagnostic_records = memory.sessions.read_all_records_strict(result.session_id)
    assert tracker.get("peak") == 2, [
        item.model_dump() for _, item in diagnostic_records if item.role == "tool"
    ]
    operations = [
        item for item in controller.operations(run_id) if item.kind is OperationKind.TOOL
    ]
    assert len(operations) == 2, (
        controller.operations(run_id), tracker, provider.count, result,
        runtime.tool_context.operation_coordinator,
    )
    assert len({item.tool_batch_id for item in operations}) == 1
    assert sorted(item.tool_call_position for item in operations) == [0, 1]
    observations = [
        controller.tool_observation(f"tool-observation:{run_id}:{call_id}")
        for call_id in ("a", "b")
    ]
    assert all(item is not None and item.state.value == "published" for item in observations)
    records = [
        record for _, record in memory.sessions.read_all_records_strict(result.session_id)
        if record.role == "tool"
    ]
    assert [record.tool_call_id for record in records] == ["a", "b"]


def test_tool_observation_materialization_is_idempotent_and_detects_conflict(
    tmp_path: Path,
) -> None:
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    project = store.register_project(tmp_path)
    controller = StateController(store.database_path, gateway_epoch="observation-epoch")
    run_id = uuid4().hex
    controller.create_run(
        run_id=run_id,
        workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id,
        client_id="observation-test",
        task="observation",
        idempotency_key=uuid4().hex,
        request_hash=hashlib.sha256(b"observation").hexdigest(),
    )
    content = "stable result"
    observation = MaterializedToolObservation(
        observation_id=f"tool-observation:{run_id}:call-a",
        run_id=run_id,
        logical_model_call_id="model-a",
        tool_call_id="call-a",
        position=0,
        name="read",
        arguments={"value": "A"},
        status="success",
        finalized_content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        created_at="2026-08-21T00:00:00+00:00",
    )

    first = controller.materialize_tool_observation(observation)
    second = controller.materialize_tool_observation(observation.model_copy(
        update={"created_at": "2026-08-21T00:00:01+00:00"},
    ))
    assert second == first

    changed = "different result"
    with pytest.raises(StateConflictError, match="content conflict"):
        controller.materialize_tool_observation(observation.model_copy(update={
            "finalized_content": changed,
            "content_hash": hashlib.sha256(changed.encode("utf-8")).hexdigest(),
        }))


def test_recovery_completes_publication_after_session_append_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    project = store.register_project(tmp_path)
    controller = StateController(store.database_path, gateway_epoch="recovery-epoch")
    run_id = uuid4().hex
    controller.create_run(
        run_id=run_id,
        workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id,
        client_id="recovery-test",
        task="recover observation",
        idempotency_key=uuid4().hex,
        request_hash=hashlib.sha256(b"recover observation").hexdigest(),
    )
    content = "already executed"
    observation_id = f"tool-observation:{run_id}:call-a"
    controller.materialize_tool_observation(MaterializedToolObservation(
        observation_id=observation_id,
        run_id=run_id,
        logical_model_call_id="model-a",
        tool_call_id="call-a",
        position=0,
        name="read",
        arguments={"value": "A"},
        status="success",
        finalized_content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        created_at="2026-08-21T00:00:00+00:00",
    ))

    memory = MemoryStore(tmp_path / ".yy" / "memory")
    hooks = HookRegistry()
    register_memory_callbacks(hooks, memory)
    durable_hooks = DurableModelHooks(controller, memory)
    durable_hooks.register(hooks)
    coordinator = DurableToolCoordinator(controller)
    session_id = "0123456789abcdef"
    original_mark = controller.mark_tool_observation_published
    mark_calls = 0

    def crash_after_session_append(selected_id: str, record_id: str):
        nonlocal mark_calls
        mark_calls += 1
        if mark_calls == 1:
            raise RuntimeError("injected crash after Session append")
        return original_mark(selected_id, record_id)

    monkeypatch.setattr(
        controller, "mark_tool_observation_published", crash_after_session_append,
    )

    async def recover() -> None:
        token = coordinator.bind(run_id)
        try:
            await hooks.emit(HookEvent(
                point=HookPoint.TRACE_START,
                session_id=session_id,
                data={"task": "recover", "new_session": True},
            ))
        finally:
            coordinator.reset(token)

    with pytest.raises(RuntimeError, match="injected crash"):
        asyncio.run(recover())
    assert controller.tool_observation(observation_id).state.value == "materialized"
    first_records = [
        record for _, record in memory.sessions.read_all_records_strict(session_id)
        if record.record_id == observation_id
    ]
    assert len(first_records) == 1

    asyncio.run(recover())
    assert controller.tool_observation(observation_id).state.value == "published"
    recovered_records = [
        record for _, record in memory.sessions.read_all_records_strict(session_id)
        if record.record_id == observation_id
    ]
    assert len(recovered_records) == 1


def test_recovery_materializes_completed_attempt_without_rerunning_tool(
    tmp_path: Path,
) -> None:
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    project = store.register_project(tmp_path)
    controller = StateController(store.database_path, gateway_epoch="rebuild-epoch")
    run_id = uuid4().hex
    state, _ = controller.create_run(
        run_id=run_id,
        workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id,
        client_id="rebuild-test",
        task="rebuild observation",
        idempotency_key=uuid4().hex,
        request_hash=hashlib.sha256(b"rebuild observation").hexdigest(),
    )
    for task_state, execution_state in (
        (TaskState.QUEUED, None),
        (TaskState.STARTING, None),
        (TaskState.RUNNING, ExecutionState.THINKING),
    ):
        state = controller.apply(TransitionCommand(
            command_id=uuid4().hex,
            run_id=run_id,
            expected_revision=state.revision,
            gateway_epoch="rebuild-epoch",
            task_state=task_state,
            execution_state=execution_state,
            reason="test setup",
        )).state

    session_id = "fedcba9876543210"
    memory = MemoryStore(tmp_path / ".yy" / "memory")
    memory.create_session("rebuild", session_id=session_id)
    memory.record_model_tool_calls(
        session_id,
        content=None,
        tool_calls=[{
            "id": "call-a",
            "type": "function",
            "function": {
                "name": "read",
                "arguments": json.dumps({"value": "A", "delay": 0.0}),
            },
        }],
        model={"provider": "test", "name": "test"},
        model_call={},
    )
    tracker: dict[str, object] = {}
    tool = _ParallelRead("read", tracker)
    coordinator = DurableToolCoordinator(controller)
    context = ToolContext(project_root=tmp_path)

    async def execute_body_only() -> None:
        token = coordinator.bind(run_id)
        try:
            operation = await coordinator.prepare(
                tool=tool,
                name=tool.name,
                arguments={"value": "A", "delay": 0.0, "fail": None},
                risk="read",
                context=context,
                tool_call_id="call-a",
                tool_batch_id="batch-a",
                tool_call_position=0,
            )
            await coordinator.begin_parallel_group()
            try:
                await coordinator.execute(
                    operation,
                    lambda: tool.run({"value": "A", "delay": 0.0}, context),
                    manage_execution_state=False,
                )
            finally:
                await coordinator.finish_parallel_group()
        finally:
            coordinator.reset(token)

    asyncio.run(execute_body_only())
    assert tracker["started"] == ["A"]
    assert controller.tool_observation(f"tool-observation:{run_id}:call-a") is None

    hooks = HookRegistry()
    register_memory_callbacks(hooks, memory)
    DurableModelHooks(controller, memory).register(hooks)

    async def recover() -> None:
        token = coordinator.bind(run_id)
        try:
            await hooks.emit(HookEvent(
                point=HookPoint.TRACE_START,
                session_id=session_id,
                data={"task": "recover"},
            ))
        finally:
            coordinator.reset(token)

    asyncio.run(recover())
    observation = controller.tool_observation(f"tool-observation:{run_id}:call-a")
    assert observation is not None
    assert observation.state.value == "published"
    assert observation.arguments == {"value": "A", "delay": 0.0}
    assert tracker["started"] == ["A"]
