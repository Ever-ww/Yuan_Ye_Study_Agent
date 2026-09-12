from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.resource_adapters import RuntimeHookCallbackContribution
from Agent.resources import (
    FileTreeRuntimeResourceProvider,
    RuntimeContributionKind,
    RuntimePluginManager,
    RuntimePluginFailureKind,
    RuntimeProfile,
    RuntimePluginWatcher,
    RuntimeResourceBundle,
    default_runtime_resource_providers,
    load_generation_tool_module,
    register_runtime_resource_callbacks,
)
from Agent.state import WorkloadKind
from gateway.state_controller import StateController
from gateway.store import GatewayStore


def _manager(tmp_path: Path) -> tuple[RuntimePluginManager, Path]:
    source = tmp_path / "source"
    agent = tmp_path / "agent"
    tools = source / "tools"
    tools.mkdir(parents=True)
    agent.mkdir()
    (tools / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    provider = FileTreeRuntimeResourceProvider(
        "test.tools",
        tools,
        root_kind="source",
        contribution_kind=RuntimeContributionKind.TOOL,
        profiles=(RuntimeProfile.INTERACTIVE,),
        relative_to=source,
    )
    controller = StateController(
        GatewayStore(agent).database_path,
        gateway_epoch="runtime-resource-test",
    )
    return RuntimePluginManager(
        source_root=source,
        agent_root=agent,
        state_controller=controller,
        providers=(provider,),
    ), source


def test_generation_is_immutable_and_semantic_noop_ignores_comments(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    initial = manager.ensure_initial_generation()
    initial_bytes = (initial.artifact_root / "source" / "tools" / "__init__.py").read_bytes()

    (source / "tools" / "__init__.py").write_text(
        "# formatting-only change\nVALUE = 1\n", encoding="utf-8",
    )
    unchanged = manager.reload(actor="test")

    assert unchanged.status == "unchanged"
    assert unchanged.generation_id == initial.generation_id
    assert (initial.artifact_root / "source" / "tools" / "__init__.py").read_bytes() == initial_bytes


def test_reload_activates_atomically_and_rollback_publishes_new_generation(
    tmp_path: Path,
) -> None:
    manager, source = _manager(tmp_path)
    first = manager.ensure_initial_generation()
    first_snapshot = manager.snapshot(RuntimeProfile.INTERACTIVE)

    (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    proposed = manager.reload(actor="test")
    assert proposed.status == "awaiting_approval"
    assert manager.snapshot(RuntimeProfile.INTERACTIVE).generation_id == first.generation_id

    activated = manager.reload(actor="test", approved_plan_hash=proposed.plan_hash)
    assert activated.status == "activated"
    second_snapshot = manager.snapshot(RuntimeProfile.INTERACTIVE)
    assert second_snapshot.generation_id != first_snapshot.generation_id
    assert (first_snapshot.source_root / "tools" / "__init__.py").read_text(
        encoding="utf-8",
    ) == "VALUE = 1\n"
    assert (second_snapshot.source_root / "tools" / "__init__.py").read_text(
        encoding="utf-8",
    ) == "VALUE = 2\n"
    first_module = load_generation_tool_module(first_snapshot)
    second_module = load_generation_tool_module(second_snapshot)
    assert first_module is not second_module
    assert first_module.VALUE == 1
    assert second_module.VALUE == 2

    rollback = manager.rollback_member(
        "test.tools", from_generation_id=first.generation_id, actor="test",
    )
    assert rollback.status == "activated"
    assert rollback.generation_id not in {first.generation_id, second_snapshot.generation_id}
    rolled_back = manager.snapshot(RuntimeProfile.INTERACTIVE)
    assert (rolled_back.source_root / "tools" / "__init__.py").read_text(
        encoding="utf-8",
    ) == "VALUE = 1\n"


def test_approval_is_bound_to_exact_reload_plan(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    manager.ensure_initial_generation()
    (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    first_plan = manager.reload(actor="test")
    (source / "tools" / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")

    changed_again = manager.reload(actor="test", approved_plan_hash=first_plan.plan_hash)

    assert changed_again.status == "awaiting_approval"
    assert changed_again.plan_hash != first_plan.plan_hash


def test_exact_reload_approval_survives_manager_restart(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    manager.ensure_initial_generation()
    (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    pending = manager.reload(actor="operator")
    assert pending.status == "awaiting_approval"
    plan = manager.plan_reload()
    manager.state_controller.approve_runtime_reload_plan(plan, actor="operator")

    restarted = RuntimePluginManager(
        source_root=manager.source_root,
        agent_root=manager.agent_root,
        state_controller=manager.state_controller,
        providers=manager.providers,
    )
    result = restarted.reload(actor="gateway:recovery")
    assert result.status == "activated"
    assert result.plan_hash == pending.plan_hash


def test_snapshot_contains_profile_resolved_contribution_contract(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    manager.ensure_initial_generation()
    snapshot = manager.snapshot(RuntimeProfile.INTERACTIVE)

    assert snapshot.descriptor_ids == ("test.tools",)
    assert len(snapshot.contributions) == 1
    contribution = snapshot.contributions[0]
    assert contribution.plugin_id == "test.tools"
    assert contribution.kind is RuntimeContributionKind.TOOL
    assert contribution.source_hash
    assert contribution.semantic_hash


def test_harness_providers_are_common_plus_exact_trigger() -> None:
    root = Path(__file__).resolve().parents[1]
    providers = default_runtime_resource_providers(root, root / ".test-agent-root")
    descriptors = {
        item.plugin_id: item
        for provider in providers
        for item in provider.discover()
    }

    manual = {
        plugin_id for plugin_id, descriptor in descriptors.items()
        if any(
            RuntimeProfile.HARNESS_MANUAL in contribution.profiles
            for contribution in descriptor.contributions
        )
    }
    assert manual == {
        "harness.skills.common",
        "harness.skills.manual",
        "harness.tools.common",
        "harness.tools.manual",
        "runtime.adapter.memory",
        "runtime.adapter.sandbox",
        "runtime.adapter.dynamic-context",
    }
    assert all("capability" not in item and "dream" not in item for item in manual)

    for trigger, profile in {
        "manual": RuntimeProfile.HARNESS_MANUAL,
        "error": RuntimeProfile.HARNESS_ERROR,
        "capability": RuntimeProfile.HARNESS_CAPABILITY,
        "dream": RuntimeProfile.HARNESS_DREAM,
    }.items():
        selected = {
            plugin_id for plugin_id, descriptor in descriptors.items()
            if any(profile in item.profiles for item in descriptor.contributions)
        }
        assert f"harness.tools.{trigger}" in selected
        assert f"harness.skills.{trigger}" in selected
        assert all(
            not item.startswith("harness.tools.")
            or item in {"harness.tools.common", f"harness.tools.{trigger}"}
            for item in selected
        )
        assert all(
            not item.startswith("harness.skills.")
            or item in {"harness.skills.common", f"harness.skills.{trigger}"}
            for item in selected
        )


def test_dynamic_context_and_core_adapters_are_profile_contributions(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    agent = tmp_path / "agent"
    agent.mkdir()
    controller = StateController(GatewayStore(agent).database_path, gateway_epoch="adapters")
    providers = tuple(
        provider for provider in default_runtime_resource_providers(root, agent)
        if provider.provider_id.startswith("runtime.adapter.")
    )
    manager = RuntimePluginManager(
        source_root=root, agent_root=agent, state_controller=controller,
        providers=providers,
    )
    manager.ensure_initial_generation()
    interactive = manager.snapshot(RuntimeProfile.INTERACTIVE)
    identities = {(item.kind, item.contract.get("adapter")) for item in interactive.contributions}
    assert (RuntimeContributionKind.HOOK, "memory") in identities
    assert (RuntimeContributionKind.HOOK, "sandbox") in identities
    assert (RuntimeContributionKind.DYNAMIC_CONTEXT, "dynamic_context") in identities


def test_profiles_expose_only_their_authorized_resource_surface() -> None:
    root = Path(__file__).resolve().parents[1]
    descriptors = {
        descriptor.plugin_id: descriptor
        for provider in default_runtime_resource_providers(root, root / ".test-agent-root")
        for descriptor in provider.discover()
    }

    def visible(profile: RuntimeProfile) -> set[str]:
        return {
            plugin_id for plugin_id, descriptor in descriptors.items()
            if any(profile in item.profiles for item in descriptor.contributions)
        }

    assert "builtin.extensions" not in visible(RuntimeProfile.CRON)
    assert not any(item.startswith("harness.") for item in visible(RuntimeProfile.CRON))
    assert visible(RuntimeProfile.COMPRESSION) == set()
    assert visible(RuntimeProfile.MAINTENANCE) == {
        "runtime.adapter.memory", "runtime.adapter.dynamic-context",
    }
    assert not any(item.startswith("harness.") for item in visible(RuntimeProfile.INTERACTIVE))


def test_plugin_hook_callback_uses_stable_executor_and_classifies_failures() -> None:
    failures: list[tuple[str, RuntimePluginFailureKind]] = []
    successes: list[str] = []

    async def broken(event: HookEvent) -> None:
        del event
        raise RuntimeError("plugin bug")

    adapter = RuntimeHookCallbackContribution(
        plugin_id="test.hook", name="broken", point=HookPoint.MODEL_BEFORE,
        callback=broken,
        failure_reporter=lambda plugin_id, kind, error: failures.append((plugin_id, kind)),
        success_reporter=successes.append,
    )
    hooks = HookRegistry()
    adapter.install(hooks)

    # The callback is isolated by the immutable HookExecutor rather than
    # replacing the dispatcher or terminating the Agent turn.
    asyncio.run(hooks.emit(HookEvent(
        point=HookPoint.MODEL_BEFORE, session_id="session", data={},
    )))
    assert failures == [("test.hook", RuntimePluginFailureKind.PLUGIN_EXCEPTION)]
    assert successes == []


def test_only_plugin_faults_quarantine_and_publish_new_rollback_generation(
    tmp_path: Path,
) -> None:
    manager, source = _manager(tmp_path)
    first = manager.ensure_initial_generation()
    (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    proposal = manager.reload(actor="test")
    manager.reload(actor="test", approved_plan_hash=proposal.plan_hash)
    bad = manager.snapshot(RuntimeProfile.INTERACTIVE)

    for _ in range(4):
        assert manager.report_failure(
            generation_id=bad.generation_id,
            plugin_id="test.tools",
            profile=RuntimeProfile.INTERACTIVE,
            failure_kind=RuntimePluginFailureKind.BUSINESS_FAILURE,
            error="domain result",
        ) is None
    assert manager.snapshot(RuntimeProfile.INTERACTIVE).generation_id == bad.generation_id

    assert manager.report_failure(
        generation_id=bad.generation_id, plugin_id="test.tools",
        profile=RuntimeProfile.INTERACTIVE,
        failure_kind=RuntimePluginFailureKind.PLUGIN_EXCEPTION, error="broken",
    ) is None
    assert manager.report_failure(
        generation_id=bad.generation_id, plugin_id="test.tools",
        profile=RuntimeProfile.INTERACTIVE,
        failure_kind=RuntimePluginFailureKind.PLUGIN_TIMEOUT, error="slow",
    ) is None
    rollback = manager.report_failure(
        generation_id=bad.generation_id, plugin_id="test.tools",
        profile=RuntimeProfile.INTERACTIVE,
        failure_kind=RuntimePluginFailureKind.CONTRACT_VIOLATION, error="invalid",
    )
    assert rollback is not None and rollback.status == "activated"
    restored = manager.snapshot(RuntimeProfile.INTERACTIVE)
    assert restored.generation_id not in {first.generation_id, bad.generation_id}
    assert (restored.source_root / "tools" / "__init__.py").read_text(
        encoding="utf-8",
    ) == "VALUE = 1\n"
    assert manager.state_controller.runtime_generation(bad.generation_id)["status"] == "quarantined"


def test_plugin_success_resets_only_consecutive_failure_streak(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    generation = manager.ensure_initial_generation()
    for _ in range(2):
        manager.report_failure(
            generation_id=generation.generation_id, plugin_id="test.tools",
            profile=RuntimeProfile.INTERACTIVE,
            failure_kind=RuntimePluginFailureKind.PLUGIN_EXCEPTION, error="broken",
        )
    manager.report_success(
        generation_id=generation.generation_id, plugin_id="test.tools",
        profile=RuntimeProfile.INTERACTIVE,
    )
    manager.report_failure(
        generation_id=generation.generation_id, plugin_id="test.tools",
        profile=RuntimeProfile.INTERACTIVE,
        failure_kind=RuntimePluginFailureKind.PLUGIN_EXCEPTION, error="broken again",
    )
    status = manager.state_controller.runtime_resource_status()
    assert status["quarantined_plugins"] == []


def test_run_reference_restores_exact_generation_after_new_activation(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    first = manager.ensure_initial_generation()
    manager.acquire_reference(first.generation_id, owner_kind="run", owner_id="run-1")
    (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    proposal = manager.reload(actor="test")
    manager.reload(actor="test", approved_plan_hash=proposal.plan_hash)

    bound = manager.referenced_generation(owner_kind="run", owner_id="run-1")
    recovered = manager.snapshot(RuntimeProfile.INTERACTIVE, generation_id=bound)
    current = manager.snapshot(RuntimeProfile.INTERACTIVE)
    assert recovered.generation_id == first.generation_id
    assert current.generation_id != recovered.generation_id


def test_run_creation_atomically_binds_runtime_generation(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    generation = manager.ensure_initial_generation()
    manager.state_controller.create_run(
        run_id="run-atomic-generation",
        workload_kind=WorkloadKind.CHAT,
        project_id="project",
        client_id="client",
        task="task",
        idempotency_key="run-atomic-generation",
        request_hash="a" * 64,
        runtime_generation_id=generation.generation_id,
    )
    assert manager.referenced_generation(
        owner_kind="run", owner_id="run-atomic-generation",
    ) == generation.generation_id


def test_generation_reference_blocks_artifact_gc(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    first = manager.ensure_initial_generation()
    (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    proposal = manager.reload(actor="test")
    manager.reload(actor="test", approved_plan_hash=proposal.plan_hash)
    with manager.state_controller._connection() as connection:
        connection.execute(
            "UPDATE runtime_resource_generations SET retired_at='2000-01-01T00:00:00+00:00' "
            "WHERE generation_id=?",
            (first.generation_id,),
        )
    manager.acquire_reference(
        first.generation_id, owner_kind="recovery", owner_id="recovery-1",
    )
    assert manager.collect_garbage(retention_days=1) == ()
    assert first.artifact_root.is_dir()

    manager.release_reference(owner_kind="recovery", owner_id="recovery-1")
    assert manager.collect_garbage(retention_days=1) == (first.generation_id,)
    assert not first.artifact_root.exists()
    assert manager.state_controller.runtime_generation(first.generation_id) is not None


def test_generation_gc_resumes_exact_fenced_artifact_after_crash(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    first = manager.ensure_initial_generation()
    (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    proposal = manager.reload(actor="test")
    manager.reload(actor="test", approved_plan_hash=proposal.plan_hash)
    assert manager.state_controller.delete_runtime_generation_if_unreferenced(
        first.generation_id,
    ) is True
    assert first.artifact_root.is_dir()

    assert manager.collect_garbage(retention_days=3650) == (first.generation_id,)
    assert not first.artifact_root.exists()


def test_watcher_quiesce_prevents_discovery_until_resume(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    manager.ensure_initial_generation()
    watcher = RuntimePluginWatcher(manager, poll_seconds=0.01, debounce_seconds=0.02)

    async def scenario() -> None:
        await watcher.quiesce(7)
        await watcher.start()
        (source / "tools" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.sleep(0.15)
        assert watcher.status()["last_source_hash"] is None
        await watcher.resume(7)
        await asyncio.sleep(0.25)
        assert watcher.status()["last_source_hash"] is not None
        await watcher.close()

    asyncio.run(scenario())


def test_harness_snapshot_file_view_excludes_other_triggers(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    agent = tmp_path / "agent"
    agent.mkdir()
    providers = tuple(
        provider for provider in default_runtime_resource_providers(root, agent)
        if provider.provider_id.startswith("harness.")
    )
    controller = StateController(
        GatewayStore(agent).database_path,
        gateway_epoch="harness-resource-test",
    )
    manager = RuntimePluginManager(
        source_root=root,
        agent_root=agent,
        state_controller=controller,
        providers=providers,
    )
    manager.ensure_initial_generation()

    manual = manager.snapshot(RuntimeProfile.HARNESS_MANUAL)
    assert (manual.source_root / "tools" / "common").is_dir()
    assert (manual.source_root / "tools" / "manual").is_dir()
    assert not (manual.source_root / "tools" / "capability").exists()
    assert not (manual.source_root / "skills" / "error").exists()


def test_hook_bus_mounts_and_unmounts_frozen_bundle() -> None:
    class Registry:
        def __init__(self) -> None:
            self.frozen = False

        def freeze(self) -> None:
            self.frozen = True

    class Target:
        def __init__(self) -> None:
            self.events: list[str] = []

        def mount_runtime_resources(self, bundle, event) -> None:
            bundle.tools.freeze()
            self.events.append(f"mount:{event.session_id}")

        def unmount_runtime_resources(self, bundle, event) -> None:
            self.events.append(f"unmount:{event.session_id}")

    manager_root = Path(__file__).resolve().parents[1]
    # Snapshot fields are identity-only here; concrete execution stays in Registry.
    from Agent.resources import RuntimeResourceSnapshot

    digest = "0" * 64
    snapshot = RuntimeResourceSnapshot(
        generation_id=digest,
        profile=RuntimeProfile.INTERACTIVE,
        descriptor_ids=(),
        source_root=manager_root,
        agent_root=manager_root,
        tool_catalog_hash=digest,
        skill_catalog_hash=digest,
        prompt_hash=digest,
        hook_plan_hash=digest,
    )
    hooks = HookRegistry()
    tools = Registry()
    bundle = RuntimeResourceBundle(snapshot, tools, None, None, hooks, None)
    target = Target()
    register_runtime_resource_callbacks(hooks, bundle, target)

    asyncio.run(hooks.emit(HookEvent(
        point=HookPoint.TRACE_START, session_id="session", data={},
    )))
    assert tools.frozen is True
    asyncio.run(hooks.emit(HookEvent(
        point=HookPoint.TRACE_END, session_id="session", data={},
    )))
    assert target.events == ["mount:session", "unmount:session"]


def test_provider_ignores_python_cache_artifacts(tmp_path: Path) -> None:
    manager, source = _manager(tmp_path)
    cache = source / "tools" / "__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"unstable")
    generation = manager.ensure_initial_generation()

    files = generation.descriptors[0].files
    assert all("__pycache__" not in item.path and not item.path.endswith(".pyc") for item in files)


def test_reload_provider_cannot_publish_stable_core_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    core = source / "Agent"
    agent = tmp_path / "agent"
    core.mkdir(parents=True)
    agent.mkdir()
    (core / "hook.py").write_text("CORE = True\n", encoding="utf-8")
    provider = FileTreeRuntimeResourceProvider(
        "bad.core",
        core,
        root_kind="source",
        contribution_kind=RuntimeContributionKind.HOOK,
        profiles=(RuntimeProfile.INTERACTIVE,),
        relative_to=source,
    )
    controller = StateController(
        GatewayStore(agent).database_path, gateway_epoch="stable-core-test",
    )
    manager = RuntimePluginManager(
        source_root=source,
        agent_root=agent,
        state_controller=controller,
        providers=(provider,),
    )

    with pytest.raises(ValueError, match="Stable Core"):
        manager.discover()
