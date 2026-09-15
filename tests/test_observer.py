from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path
from uuid import uuid4

import pytest

from Agent.config import load_runtime_config
from Agent.contracts import ModelReply
from Agent.observer import (
    IntentAlignment, IntentAlignmentStatus, ObserverState,
    render_observer_progress,
)
from backup.maintenance import AgentHomeWriteGate, MaintenanceBlockedError
from Agent.resources import (
    FileTreeRuntimeResourceProvider,
    RuntimeContributionKind,
    RuntimePluginManager,
    RuntimeProfile,
    default_runtime_resource_providers,
)
from Agent.state import RecordRuntimeEventCommand, WorkloadKind
from gateway.event_store import EventStore
from gateway.application import GatewayApplication
from gateway.models import GatewayEventEnvelope
from gateway.observer import (
    GatewayObserverService,
    ObserverPluginTimeout,
    ObserverStateStore,
    VisibleEventProjection,
)
from gateway.runtime_pool import RuntimePool
from gateway.state_controller import StateController
from gateway.store import GatewayStore


class _ObserverTestProvider:
    streaming = False

    async def complete(self, messages, tools):
        assert tools == []
        payload = json.loads(next(
            str(item["content"]) for item in reversed(messages) if item["role"] == "user"
        ))
        previous = payload["previous_state"]
        event = payload["visible_event"]
        completed = list(previous["completed_tasks"])
        if event["event_type"] == "tool_completed" and event.get("tool_name"):
            completed.append(f"工具 {event['tool_name']}：{event.get('tool_status') or 'completed'}")
        if event["event_type"] in {"final", "run_completed"} and event.get("content"):
            completed.append(event["content"])
        terminal = bool(payload["terminal"])
        return ModelReply(text=json.dumps({
            "user_problem": previous["user_problem"],
            "completed_tasks": completed[-20:],
            "in_progress_task": "" if terminal else "处理当前请求",
            "current_agent_action": "已完成" if terminal else "分析可见事件",
            "intent_alignment": {
                "status": "drifted" if "FORCE_DRIFT" in event.get("content", "") else "aligned",
                "reason": "final result drifted" if "FORCE_DRIFT" in event.get("content", "") else "",
            },
        }, ensure_ascii=False))

    async def stream(self, messages, tools):
        yield await self.complete(messages, tools)


def _setup(tmp_path: Path, provider_factory=_ObserverTestProvider):
    source = tmp_path / "source"
    agent = tmp_path / "agent"
    (tmp_path / "workspace").mkdir()
    plugin_root = source / "observer_plugins"
    plugin_root.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[1]
    shutil.copy2(repository / "observer_plugins" / "__init__.py", plugin_root / "__init__.py")
    shutil.copy2(repository / "observer_plugins" / "default.py", plugin_root / "default.py")
    store = GatewayStore(agent)
    project = store.register_project(tmp_path / "workspace")
    controller = StateController(store.database_path, gateway_epoch="observer-test")
    provider = FileTreeRuntimeResourceProvider(
        "builtin.observer", plugin_root, root_kind="source",
        contribution_kind=RuntimeContributionKind.OBSERVER,
        profiles=(RuntimeProfile.INTERACTIVE,), relative_to=source,
    )
    manager = RuntimePluginManager(
        source_root=source, agent_root=agent, state_controller=controller,
        providers=(provider,),
    )
    generation = manager.ensure_initial_generation()
    run_id = uuid4().hex
    state, _ = controller.create_run(
        run_id=run_id, workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id, client_id="client", task="研究项目结构",
        idempotency_key=run_id, request_hash=hashlib.sha256(b"task").hexdigest(),
        runtime_generation_id=generation.generation_id,
    )
    observer_store = ObserverStateStore(store.database_path)
    config = load_runtime_config(agent, workspace_root=tmp_path / "workspace")
    service = GatewayObserverService(
        event_store=EventStore(store.database_path), state_store=observer_store,
        gateway_store=store, resource_manager=manager, config=config,
        timeout_seconds=60, provider_factory=provider_factory,
    )
    return store, controller, manager, state, observer_store, service


def _event(controller: StateController, run_id: str, event_type: str, payload: dict):
    state = controller.state(run_id)
    return controller.apply(RecordRuntimeEventCommand(
        command_id=uuid4().hex, run_id=run_id, expected_revision=state.revision,
        gateway_epoch=controller.gateway_epoch, event_type=event_type, payload=payload,
    ))


def test_observer_consumes_only_visible_projection_and_commits_offset(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    result = _event(controller, state.run_id, "tool_requested", {
        "name": "read_file", "arguments": {"secret": "must-not-leak"},
        "loop": 2, "execution": "parallel",
    })
    outputs = service.observe_event(str(result.event_id))

    status = observer_store.status(state.run_id)
    assert status["last_event_offset"] >= 2
    assert status["tool_loops"] == [{
        "loop": 2, "execution": "parallel", "tools": ["read_file"],
    }]
    assert outputs[-1].event_type == "observer_progress"
    with observer_store._connect() as connection:
        raw = "\n".join(
            str(row[0] or "") for row in connection.execute(
                "SELECT visible_event_json FROM observer_visible_events WHERE run_id=?",
                (state.run_id,),
            )
        )
    assert "must-not-leak" not in raw


def test_observer_does_not_use_gateway_process_home_as_workspace(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    service.model_runtime.config = service.model_runtime.config.model_copy(update={
        "workspace_root": Path.home(),
    })
    result = _event(controller, state.run_id, "text", {"content": "正在回答"})

    service.observe_event(str(result.event_id))

    status = observer_store.status(state.run_id)
    assert status["status"] == "active"
    assert status["last_event_offset"] >= 2


def test_progress_output_never_reenters_observer_input(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    source = _event(controller, state.run_id, "text", {"content": "正在读取源码"})
    service.observe_event(str(source.event_id))
    progress = _event(controller, state.run_id, "observer_progress", {
        "progress_markdown": "internal projection",
    })
    service.observe_event(str(progress.event_id))
    with observer_store._connect() as connection:
        row = connection.execute(
            "SELECT visible FROM observer_visible_events WHERE event_id=?", (progress.event_id,),
        ).fetchone()
    assert row[0] == 0


def test_terminal_event_finalizes_profile_scoped_evidence(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    started = _event(controller, state.run_id, "run_started", {"message": "started"})
    service.observe_event(str(started.event_id))
    terminal = _event(controller, state.run_id, "run_completed", {"answer": "结构分析完成"})
    outputs = service.observe_event(str(terminal.event_id))

    evidence = observer_store.finalized_evidence()
    assert len(evidence) == 1
    assert evidence[0].runtime_profile == "interactive"
    assert evidence[0].agent_role == "main"
    assert evidence[0].user_problem == "研究项目结构"
    # Evidence is durable Observer state, not another item appended after the
    # normal Run terminal event in the chat timeline.
    assert outputs == ()


def test_streaming_text_is_evidence_not_an_observer_model_milestone(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    intermediate = _event(
        controller, state.run_id, "text", {"content": "FORCE_DRIFT intermediate"},
    )
    outputs = service.observe_event(str(intermediate.event_id))
    assert all(item.event_type != "observer_correction_proposed" for item in outputs)
    # Per-token text remains visible durable evidence, but does not trigger an
    # Observer LLM request. The terminal full answer makes the final decision.
    assert observer_store.status(state.run_id)["state"]["intent_alignment"]["status"] == "aligned"

    terminal = _event(
        controller, state.run_id, "run_completed", {"answer": "正常完成用户目标"},
    )
    outputs = service.observe_event(str(terminal.event_id))
    assert all(item.event_type != "observer_correction_proposed" for item in outputs)
    status = observer_store.status(state.run_id)
    assert status["state"]["intent_alignment"]["status"] == "aligned"
    assert status["correction_proposal"] is None


def test_observer_schedules_only_coarse_visible_milestones() -> None:
    assert GatewayObserverService.accepts_event_type("text") is False
    assert GatewayObserverService.accepts_event_type("final") is False
    assert GatewayObserverService.accepts_event_type("run_started") is True
    assert GatewayObserverService.accepts_event_type("tool_requested") is True
    assert GatewayObserverService.accepts_event_type("run_completed") is True
    # The durable snapshot transition precedes the actual user-facing result;
    # it must not finalize the Observer early.
    assert GatewayObserverService.accepts_event_type("run_terminal") is False


def test_internal_run_terminal_does_not_mask_successful_completion(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    internal = _event(controller, state.run_id, "run_terminal", {
        "task_state": "succeeded",
    })
    completed = _event(controller, state.run_id, "run_completed", {
        "answer": "completed normally",
    })

    # Processing the later public terminal event also consumes the intervening
    # internal event, but only run_completed may finalize the Observer.
    assert service.observe_event(str(completed.event_id)) == ()
    status = observer_store.status(state.run_id)
    assert status["status"] == "finalized"
    assert "completed normally" in status["state"]["completed_tasks"]
    assert "\u26a0" not in status["progress_markdown"]
    internal_record = service.event_store.read_canonical(str(internal.event_id)).envelope
    assert int(status["last_event_offset"]) >= int(
        internal_record.stream_sequence or internal_record.sequence
    )


def test_final_observer_progress_collapses_to_one_turn_result() -> None:
    aligned = ObserverState(
        user_problem="检查源码",
        completed_tasks=("读取文件", "执行测试"),
        current_agent_action="已完成",
    )
    terminal = render_observer_progress(
        aligned, terminal_event_type="run_completed",
    )
    assert terminal.startswith("✓ 任务已完成")
    assert "读取文件" in terminal
    assert "执行测试" in terminal
    assert "用户问题" in render_observer_progress(aligned)

    drifted = aligned.model_copy(update={
        "intent_alignment": IntentAlignment(
            status=IntentAlignmentStatus.DRIFTED,
            reason="偏离目标",
        ),
    })
    assert "意图偏移" in render_observer_progress(
        drifted, terminal_event_type="run_completed",
    )


def test_runtime_pool_queues_observer_without_blocking_main_output() -> None:
    class SlowObserver:
        def observe_event(self, event_id: str):
            time.sleep(0.15)
            return ()

        def record_failure(self, run_id: str, error: Exception) -> None:
            raise AssertionError((run_id, error))

    async def check() -> None:
        pool = object.__new__(RuntimePool)
        pool.observer_service = SlowObserver()
        pool._observer_tasks = set()
        pool._observer_chains = {}
        pool._closing = False
        pool.outbox = None
        started = time.perf_counter()
        pool._schedule_observer("run", "event")
        assert time.perf_counter() - started < 0.05
        await asyncio.gather(*tuple(pool._observer_tasks))

    asyncio.run(check())


def test_terminal_llm_state_drift_creates_correction(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    terminal = _event(
        controller, state.run_id, "run_completed", {"answer": "FORCE_DRIFT final"},
    )
    outputs = service.observe_event(str(terminal.event_id))
    assert [item.event_type for item in outputs] == ["observer_correction_proposed"]
    assert observer_store.status(state.run_id)["correction_proposal"]["status"] == "pending"


def test_skill_evidence_is_rule_derived_not_llm_completed_tasks(tmp_path: Path) -> None:
    class HallucinatingProvider(_ObserverTestProvider):
        async def complete(self, messages, tools):
            reply = await super().complete(messages, tools)
            value = json.loads(reply.text)
            value["completed_tasks"] = ["hidden model-only claim"]
            return ModelReply(text=json.dumps(value, ensure_ascii=False))

    _, controller, _, state, observer_store, service = _setup(
        tmp_path, HallucinatingProvider,
    )
    service.observe_event(_event(
        controller, state.run_id, "tool_completed",
        {"name": "read_file", "status": "success"},
    ).event_id)
    service.observe_event(_event(
        controller, state.run_id, "run_completed", {"answer": "可见结果"},
    ).event_id)
    evidence = observer_store.finalized_evidence()[0]
    assert "hidden model-only claim" not in evidence.completed_tasks
    assert any("read_file" in item for item in evidence.completed_tasks)
    assert "可见结果" in evidence.completed_tasks


def test_drift_creates_cas_proposal_and_decision_finalizes_evidence(tmp_path: Path) -> None:
    store, controller, manager, state, observer_store, service = _setup(tmp_path)
    event_result = _event(controller, state.run_id, "run_started", {"message": "started"})
    service.observe_event(str(event_result.event_id))
    with observer_store._connect() as connection:
        row = connection.execute(
            "SELECT state_json FROM observer_instances WHERE run_id=?", (state.run_id,),
        ).fetchone()
        previous = ObserverState.model_validate_json(row[0], strict=True)
        drifted = previous.model_copy(update={
            "intent_alignment": IntentAlignment(
                status=IntentAlignmentStatus.DRIFTED, reason="当前动作偏离用户目标",
            )
        })
        connection.execute(
            "UPDATE observer_instances SET state_json=? WHERE run_id=?",
            (drifted.model_dump_json(), state.run_id),
        )
        connection.commit()
    _, proposal = observer_store.finalize_or_propose(state.run_id, timeout_seconds=60)
    assert proposal is not None and proposal["status"] == "pending"
    evidence = observer_store.decide_correction(
        str(proposal["proposal_id"]), expected_revision=0,
        action="edit", actor="user", edited_prompt="只分析项目结构", reason="edited",
    )
    assert evidence.adopted_correction_prompt == "只分析项目结构"
    assert observer_store.status(state.run_id)["status"] == "finalized"


def test_skill_candidate_requires_repeated_finalized_evidence(tmp_path: Path) -> None:
    _, controller, _, first, observer_store, service = _setup(tmp_path)
    # Three distinct Runs are required; one ordinary execution cannot publish a Skill.
    for index in range(3):
        if index == 0:
            run_id = first.run_id
        else:
            generation_id = service.resource_manager.snapshot(RuntimeProfile.INTERACTIVE).generation_id
            run_id = uuid4().hex
            service.state_store  # keep the same Core store
            controller.create_run(
                run_id=run_id, workload_kind=WorkloadKind.CHAT,
                project_id=service.gateway_store.list_projects()[0].project_id,
                client_id="client", task="研究项目结构", idempotency_key=run_id,
                request_hash=hashlib.sha256(str(index).encode()).hexdigest(),
                runtime_generation_id=generation_id,
            )
        terminal = _event(controller, run_id, "run_completed", {"answer": "结构分析完成"})
        service.observe_event(str(terminal.event_id))
        created = observer_store.create_skill_candidates(minimum_evidence=3)
        assert bool(created) is (index == 2)
    candidates = observer_store.skill_candidates()
    assert len(candidates) == 1
    assert candidates[0]["runtime_profile"] == "interactive"
    assert candidates[0]["status"] == "awaiting_approval"


def test_skill_candidate_does_not_use_generic_fallback_without_a_pattern(
    tmp_path: Path,
) -> None:
    _, controller, _, first, observer_store, service = _setup(tmp_path)
    for index, answer in enumerate((
        "完成数据库迁移分析", "写完用户界面说明", "验证网络重试策略",
    )):
        run_id = first.run_id if index == 0 else uuid4().hex
        if index:
            controller.create_run(
                run_id=run_id, workload_kind=WorkloadKind.CHAT,
                project_id=first.project_id, client_id="client", task=f"任务 {index}",
                idempotency_key=run_id,
                request_hash=hashlib.sha256(answer.encode()).hexdigest(),
                runtime_generation_id=service.resource_manager.snapshot(
                    RuntimeProfile.INTERACTIVE,
                ).generation_id,
            )
        terminal = _event(
            controller, run_id, "run_completed", {"answer": answer},
        )
        service.observe_event(terminal.event_id)

    assert observer_store.create_skill_candidates(minimum_evidence=3) == ()
    assert len(observer_store.finalized_evidence(unconsumed_only=True)) == 3


def test_projection_contract_rejects_hidden_event_fields() -> None:
    from gateway.models import GatewayEventEnvelope

    event = GatewayEventEnvelope(
        version=2, event_id="event", sequence=1, timestamp="2026-01-01T00:00:00+00:00",
        project_id="project", run_id="run", type="tool_completed",
        payload={"name": "read_file", "content": "secret result", "arguments": {"x": 1}},
        command_id="command", event_key="primary", stream_id="run", stream_sequence=1,
        event_type="tool_completed", schema_version=1,
    )
    visible = VisibleEventProjection.project(event)
    assert visible is not None
    assert "secret result" not in visible.model_dump_json()
    assert "arguments" not in visible.model_dump_json()


def test_recovery_continues_from_durable_offset_without_resummarizing(tmp_path: Path) -> None:
    store, controller, manager, state, observer_store, service = _setup(tmp_path)
    first = _event(controller, state.run_id, "text", {"content": "第一步"})
    service.observe_event(str(first.event_id))
    offset = observer_store.status(state.run_id)["last_event_offset"]
    second = _event(controller, state.run_id, "tool_requested", {
        "name": "read_file", "loop": 1, "execution": "serial",
    })

    recovered = GatewayObserverService(
        event_store=EventStore(store.database_path), state_store=observer_store,
        gateway_store=store, resource_manager=manager,
        config=load_runtime_config(store.directory, workspace_root=tmp_path / "workspace"),
        timeout_seconds=60, provider_factory=_ObserverTestProvider,
    )
    recovered.recover()
    status = observer_store.status(state.run_id)
    assert status["last_event_offset"] > offset
    assert status["tool_loops"] == [{
        "loop": 1, "execution": "serial", "tools": ["read_file"],
    }]
    with observer_store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM observer_visible_events WHERE event_id=?", (second.event_id,),
        ).fetchone()[0] == 1


def test_correction_timeout_defaults_to_reject_and_finalizes(tmp_path: Path) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    started = _event(controller, state.run_id, "run_started", {"message": "started"})
    service.observe_event(str(started.event_id))
    with observer_store._connect() as connection:
        row = connection.execute(
            "SELECT state_json FROM observer_instances WHERE run_id=?", (state.run_id,),
        ).fetchone()
        current = ObserverState.model_validate_json(row[0], strict=True)
        drifted = current.model_copy(update={
            "intent_alignment": IntentAlignment(
                status=IntentAlignmentStatus.DRIFTED, reason="偏离",
            ),
        })
        connection.execute(
            "UPDATE observer_instances SET state_json=? WHERE run_id=?",
            (drifted.model_dump_json(), state.run_id),
        )
        connection.commit()
    _, proposal = observer_store.finalize_or_propose(state.run_id, timeout_seconds=-1)
    assert proposal is not None
    assert observer_store.expire_due() == (proposal["proposal_id"],)
    evidence = observer_store.finalized_evidence()[0]
    assert evidence.user_correction == "timeout_rejected"
    assert observer_store.status(state.run_id)["status"] == "finalized"


def test_cron_evolved_skill_is_absent_from_interactive_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    agent = tmp_path / "agent"
    (source / "skills" / "main-skill").mkdir(parents=True)
    (source / "skills" / "main-skill" / "SKILL.md").write_text(
        "---\nname: main-skill\ndescription: Explicit shared built-in.\n---\n", encoding="utf-8",
    )
    interactive_skill = (
        source / "runtime-resources" / "interactive" / "skills" / "interactive-skill"
    )
    interactive_skill.mkdir(parents=True)
    (interactive_skill / "SKILL.md").write_text(
        "---\nname: interactive-skill\ndescription: Interactive only.\n---\n",
        encoding="utf-8",
    )
    cron_skill = source / "runtime-resources" / "cron" / "skills" / "cron-skill"
    cron_skill.mkdir(parents=True)
    (cron_skill / "SKILL.md").write_text(
        "---\nname: cron-skill\ndescription: Cron only.\n---\n", encoding="utf-8",
    )
    agent.mkdir()
    providers = tuple(
        provider for provider in default_runtime_resource_providers(source, agent)
        if provider.provider_id in {
            "builtin.skills", "runtime.skills.interactive", "runtime.skills.cron",
        }
    )
    controller = StateController(GatewayStore(agent).database_path, gateway_epoch="profiles")
    manager = RuntimePluginManager(
        source_root=source, agent_root=agent, state_controller=controller,
        providers=providers,
    )
    manager.ensure_initial_generation()
    interactive = manager.snapshot(RuntimeProfile.INTERACTIVE)
    cron = manager.snapshot(RuntimeProfile.CRON)
    assert not (interactive.source_root / "skills" / "cron-skill").exists()
    assert (interactive.source_root / "skills" / "interactive-skill" / "SKILL.md").is_file()
    assert (cron.source_root / "skills" / "cron-skill" / "SKILL.md").is_file()
    assert not (cron.source_root / "skills" / "interactive-skill").exists()


def test_gateway_code_workload_binds_harness_observer_profile(tmp_path: Path) -> None:
    application = GatewayApplication(
        load_runtime_config(tmp_path), observer_provider_factory=_ObserverTestProvider,
    )
    state = application._begin_workload_run(
        run_id=uuid4().hex,
        workload=WorkloadKind.CODE_TURN,
        project_id="code-project",
        client_id="code-client",
        task="修改 Hook 行为",
    )
    status = application.observer_status(state.run_id)
    assert status["runtime_profile"] == "harness:manual"
    instance = application.observer.state_store.instance(state.run_id)
    assert instance is not None
    assert instance["runtime_profile"] == "harness:manual"
    assert application.runtime_plugins.referenced_generation(
        owner_kind="run", owner_id=state.run_id,
    ) == instance["generation_id"]


def test_terminal_offset_reconcile_finalizes_evidence_after_crash(
    tmp_path: Path, monkeypatch,
) -> None:
    _, controller, _, state, observer_store, service = _setup(tmp_path)
    service.observe_event(_event(
        controller, state.run_id, "text", {"content": "处理中"},
    ).event_id)
    terminal = _event(
        controller, state.run_id, "run_completed", {"answer": "结构分析完成"},
    )
    original = observer_store.finalize_or_propose

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after terminal offset commit")

    monkeypatch.setattr(observer_store, "finalize_or_propose", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.observe_event(terminal.event_id)
    monkeypatch.setattr(observer_store, "finalize_or_propose", original)

    service.recover()

    assert observer_store.status(state.run_id)["status"] == "finalized"
    assert len(observer_store.finalized_evidence()) == 1


def test_conflicting_skill_candidate_decision_is_rejected(tmp_path: Path) -> None:
    _, controller, _, first, observer_store, service = _setup(tmp_path)
    for index in range(3):
        run_id = first.run_id if index == 0 else uuid4().hex
        if index:
            controller.create_run(
                run_id=run_id, workload_kind=WorkloadKind.CHAT,
                project_id=first.project_id, client_id="client", task="研究项目结构",
                idempotency_key=run_id,
                request_hash=hashlib.sha256(str(index).encode()).hexdigest(),
                runtime_generation_id=service.resource_manager.snapshot(
                    RuntimeProfile.INTERACTIVE,
                ).generation_id,
            )
        terminal = _event(
            controller, run_id, "run_completed", {"answer": "结构分析完成"},
        )
        service.observe_event(terminal.event_id)
    candidate = observer_store.create_skill_candidates(minimum_evidence=3)[0]
    observer_store.decide_skill_candidate(
        candidate["candidate_id"], expected_revision=0, approved=False,
    )

    with pytest.raises(RuntimeError, match="already decided as rejected"):
        observer_store.decide_skill_candidate(
            candidate["candidate_id"], expected_revision=0, approved=True,
        )


def test_observer_write_is_denied_during_maintenance(tmp_path: Path) -> None:
    _, _, _, state, observer_store, _ = _setup(tmp_path)
    gate = AgentHomeWriteGate()
    guarded = ObserverStateStore(observer_store.database_path, write_gate=gate)
    asyncio.run(gate.begin_draining(1))

    with pytest.raises(MaintenanceBlockedError):
        guarded.mark_failed(state.run_id)


def test_plugin_reduce_timeout_is_bounded(tmp_path: Path) -> None:
    _, _, _, _, _, service = _setup(tmp_path)
    service.plugin_timeout_seconds = 0.01

    class SlowPlugin:
        def model_messages(self, previous, visible, *, terminal):
            del previous, visible, terminal
            time.sleep(0.2)
            return ({"role": "user", "content": "{}"},)

        def parse_state(self, raw, previous):
            del raw
            return previous

    visible = VisibleEventProjection.project(GatewayEventEnvelope(
        event_id="event-timeout", project_id="project-timeout",
        run_id="run-timeout", sequence=1,
        type="text", timestamp="2026-09-13T00:00:00+08:00",
        payload={"content": "visible"},
    ))
    assert visible is not None
    started = time.monotonic()
    with pytest.raises(ObserverPluginTimeout):
        service._reduce(SlowPlugin(), ObserverState(), visible)
    assert time.monotonic() - started < 0.15

    class HealthyPlugin:
        def model_messages(self, previous, visible, *, terminal):
            del terminal
            return ({"role": "user", "content": json.dumps({
                "previous_state": previous.model_dump(mode="json"),
                "visible_event": visible.model_dump(mode="json"), "terminal": False,
            })},)

        def parse_state(self, raw, previous):
            del raw
            return previous

    # A callback which ignores cancellation must not permanently occupy the
    # executor used by the next plugin version.
    assert service._reduce(HealthyPlugin(), ObserverState(), visible) == ObserverState()
