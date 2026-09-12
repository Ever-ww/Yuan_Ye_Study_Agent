from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

from Agent.config import load_runtime_config
from Agent.observer import IntentAlignment, IntentAlignmentStatus, ObserverState
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
from gateway.observer import GatewayObserverService, ObserverStateStore, VisibleEventProjection
from gateway.state_controller import StateController
from gateway.store import GatewayStore


def _setup(tmp_path: Path):
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
    service = GatewayObserverService(
        event_store=EventStore(store.database_path), state_store=observer_store,
        gateway_store=store, resource_manager=manager, timeout_seconds=60,
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
        gateway_store=store, resource_manager=manager, timeout_seconds=60,
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
    application = GatewayApplication(load_runtime_config(tmp_path))
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
