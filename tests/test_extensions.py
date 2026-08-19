from __future__ import annotations

import asyncio
import hashlib
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from Agent import (
    EventType,
    ExtensionContext,
    ExtensionCapability,
    ExtensionTraceSnapshot,
    ExtensionLoader,
    ExtensionRuntimeBinding,
    ExtensionServices,
    HookEvent,
    HookPoint,
    HookRegistry,
    HookFailureMode,
    HookOrigin,
    RunEvent,
    WorkloadKind,
    load_runtime_config,
    build_extension_grant_plan,
)
from tool import AsyncToolRegistry
from tools.calculator import CalculatorTool
from gateway.state_controller import StateController
from gateway.store import GatewayStore


def _write_extension(
    root: Path,
    stage: str,
    filename: str,
    name: str,
    priority: int,
    body: str = "return None",
    capabilities: tuple[str, ...] = (),
    allowed_tools: tuple[str, ...] = (),
) -> Path:
    target = root / "extension" / "hook" / stage / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(f"""\
        EXTENSION_NAME = {name!r}
        PRIORITY = {priority}
        EXTENSION_MANIFEST = {{
            "schema_version": 1,
            "capabilities": {list(capabilities)!r},
            "allowed_tools": {list(allowed_tools)!r},
            "timeout_seconds": 5.0,
        }}

        async def handle(event, context):
            {body}
        """), encoding="utf-8")
    return target


class ExtensionLoaderTests(unittest.TestCase):
    def test_multiple_files_are_ordered_and_registered(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(
                root, "turn_start", "z_last.py", "last", 10,
                "context.log('last')", capabilities=("logger.write",),
            )
            _write_extension(
                root, "turn_start", "b_second.py", "second", -10,
                "context.log('second')", capabilities=("logger.write",),
            )
            _write_extension(
                root, "turn_start", "a_first.py", "first", -10,
                "context.log('first')", capabilities=("logger.write",),
            )
            catalog = ExtensionLoader(root).scan(strict=True)
            self.assertEqual(
                [item.name for item in catalog.modules[HookPoint.TURN_START]],
                ["first", "second", "last"],
            )
            registry = HookRegistry()
            audit = []

            class Backend:
                def resolve_extension_grant(self, *args):
                    del args
                    return {
                        "grant_version": 1,
                        "granted_capabilities": ["logger.write"],
                        "granted_tools": [],
                        "tool_contract_hashes": {},
                    }

                def extension_hook_is_quarantined(self, *args):
                    del args
                    return False

                def record_extension_hook_outcome(self, *args, **kwargs):
                    del args, kwargs

                def record_extension_audit(self, snapshot, **kwargs):
                    del snapshot
                    audit.append(kwargs["result"])

            catalog.register(
                registry, provider="echo", model="echo", sandbox_enabled=False,
                services=ExtensionServices(workspace_root=root, state_backend=Backend()),
                binding=ExtensionRuntimeBinding(trace_id="trace"),
            )
            event = HookEvent(point=HookPoint.TURN_START, session_id="session", data={})
            asyncio.run(registry.emit(event))
            self.assertEqual(audit, ["first", "second", "last"])

    def test_same_capability_can_span_stages(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(root, "trace_start", "metrics.py", "metrics", 0)
            _write_extension(root, "trace_end", "metrics.py", "metrics", 0)
            catalog = ExtensionLoader(root).scan(strict=True)
            self.assertEqual(len(catalog.modules[HookPoint.TRACE_START]), 1)
            self.assertEqual(len(catalog.modules[HookPoint.TRACE_END]), 1)

    def test_duplicate_name_and_invalid_contract_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(root, "tool_after", "first.py", "duplicate", 0)
            _write_extension(root, "tool_after", "second.py", "duplicate", 1)
            with self.assertRaisesRegex(ValueError, "Duplicate Extension name"):
                ExtensionLoader(root).scan(strict=True)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(root, "model_before", "broken.py", "broken", 51)
            with self.assertRaisesRegex(ValueError, "PRIORITY"):
                ExtensionLoader(root).scan(strict=True)

    def test_repository_contains_all_ten_default_stage_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalog = ExtensionLoader(root).scan(strict=True)
        for point in HookPoint:
            path = root / "extension" / "hook" / point.value / f"{point.value}.py"
            self.assertTrue(path.is_file(), path)
            self.assertTrue(catalog.modules[point], point)

    def test_ast_security_scan_runs_before_module_import(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = _write_extension(root, "turn_start", "unsafe.py", "unsafe", 0)
            target.write_text(
                "import subprocess\nraise RuntimeError('module imported')\n"
                "EXTENSION_NAME='unsafe'\nPRIORITY=0\n"
                "EXTENSION_MANIFEST={'schema_version':1,'capabilities':[],"
                "'allowed_tools':[],'timeout_seconds':5}\n"
                "async def handle(event, context):\n    return None\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "import is not allowed"):
                ExtensionLoader(root).scan(strict=True)

    def test_ast_security_scan_rejects_context_reflection_bypasses(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(
                root, "turn_start", "private.py", "private", 0,
                "return context._services",
            )
            with self.assertRaisesRegex(ValueError, "private Context"):
                ExtensionLoader(root).scan(strict=True)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(
                root, "turn_start", "reflect.py", "reflect", 0,
                "return getattr(context, '_services')",
            )
            with self.assertRaisesRegex(ValueError, "call is not allowed: getattr"):
                ExtensionLoader(root).scan(strict=True)

    def test_timeout_above_thirty_seconds_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = _write_extension(root, "turn_start", "slow.py", "slow", 0)
            text = target.read_text(encoding="utf-8").replace(
                '"timeout_seconds": 5.0', '"timeout_seconds": 31.0',
            )
            target.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "less than or equal to 30"):
                ExtensionLoader(root).scan(strict=True)

    def test_grant_plan_classifies_capabilities_and_exact_tools(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(
                root, "model_before", "planner.py", "planner", 0,
                capabilities=("logger.write", "model.request.modify", "tool.invoke"),
                allowed_tools=("calculator",),
            )
            plan = build_extension_grant_plan(
                ExtensionLoader(root).scan(strict=True),
                AsyncToolRegistry([CalculatorTool()]),
            )
            hook = plan["hooks"][0]
            self.assertEqual(hook["auto_granted_capabilities"], ["logger.write"])
            self.assertEqual(
                hook["confirmation_required_capabilities"],
                ["model.request.modify", "tool.invoke"],
            )
            self.assertEqual(hook["tools"][0]["name"], "calculator")
            self.assertTrue(hook["tools"][0]["preapprovable"])
            self.assertNotIn("*", hook["tools"][0]["name"])


class HookExecutorReliabilityTests(unittest.TestCase):
    def test_extension_failure_is_isolated_and_core_failure_is_closed(self) -> None:
        async def scenario() -> tuple[list[str], list[str]]:
            extension_calls: list[str] = []
            outcomes: list[str] = []
            registry = HookRegistry()

            async def broken(event):
                del event
                extension_calls.append("broken")
                raise RuntimeError("broken extension")

            async def healthy(event):
                del event
                extension_calls.append("healthy")

            registry.register(
                HookPoint.TURN_START, broken, origin=HookOrigin.EXTENSION,
                failure_mode=HookFailureMode.ISOLATE,
                outcome_reporter=lambda outcome, error, duration: outcomes.append(outcome),
            )
            registry.register(
                HookPoint.TURN_START, healthy, origin=HookOrigin.EXTENSION,
                failure_mode=HookFailureMode.ISOLATE,
            )
            await registry.emit(HookEvent(
                point=HookPoint.TURN_START, session_id="session", data={},
            ))
            return extension_calls, outcomes

        calls, outcomes = asyncio.run(scenario())
        self.assertEqual(calls, ["broken", "healthy"])
        self.assertEqual(outcomes, ["exception"])

        registry = HookRegistry()
        observed: list[str] = []

        async def core_failure(event):
            del event
            raise RuntimeError("core failed")

        async def should_not_run(event):
            del event
            observed.append("ran")

        registry.register(HookPoint.TURN_START, core_failure)
        registry.register(HookPoint.TURN_START, should_not_run)
        with self.assertRaisesRegex(RuntimeError, "core failed"):
            asyncio.run(registry.emit(HookEvent(
                point=HookPoint.TURN_START, session_id="session", data={},
            )))
        self.assertEqual(observed, [])

    def test_cancelled_error_propagates_and_extension_timeout_is_isolated(self) -> None:
        registry = HookRegistry()

        async def cancelled(event):
            del event
            raise asyncio.CancelledError()

        registry.register(
            HookPoint.TURN_START, cancelled, origin=HookOrigin.EXTENSION,
            failure_mode=HookFailureMode.ISOLATE,
        )
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(registry.emit(HookEvent(
                point=HookPoint.TURN_START, session_id="session", data={},
            )))

        calls: list[str] = []
        registry = HookRegistry()

        async def slow(event):
            del event
            await asyncio.sleep(0.05)

        async def healthy(event):
            del event
            calls.append("healthy")

        registry.register(
            HookPoint.TURN_START, slow, origin=HookOrigin.EXTENSION,
            timeout_seconds=0.001,
        )
        registry.register(
            HookPoint.TURN_START, healthy, origin=HookOrigin.EXTENSION,
        )
        asyncio.run(registry.emit(HookEvent(
            point=HookPoint.TURN_START, session_id="session", data={},
        )))
        self.assertEqual(calls, ["healthy"])

        core_registry = HookRegistry()
        core_registry.register(
            HookPoint.TURN_START, slow, timeout_seconds=0.001,
        )
        with self.assertRaisesRegex(RuntimeError, "failed"):
            asyncio.run(core_registry.emit(HookEvent(
                point=HookPoint.TURN_START, session_id="session", data={},
            )))

    def test_failed_extension_discards_mutation_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = _write_extension(
                root, "model_before", "broken_patch.py", "broken-patch", 0,
                capabilities=("model.request.modify",),
            )
            target.write_text(
                "EXTENSION_NAME='broken-patch'\nPRIORITY=0\n"
                "EXTENSION_MANIFEST={'schema_version':1,"
                "'capabilities':['model.request.modify'],'allowed_tools':[],"
                "'timeout_seconds':5}\n"
                "async def handle(event, context):\n"
                "    context.replace_model_messages([{'role':'user','content':'changed'}])\n"
                "    raise RuntimeError('after patch')\n",
                encoding="utf-8",
            )

            class Backend:
                def resolve_extension_grant(self, *args):
                    del args
                    return {
                        "grant_version": 1,
                        "granted_capabilities": ["model.request.modify"],
                        "granted_tools": [],
                        "tool_contract_hashes": {},
                    }

                def extension_hook_is_quarantined(self, *args):
                    del args
                    return False

                def record_extension_hook_outcome(self, *args, **kwargs):
                    del args, kwargs

            registry = HookRegistry()
            ExtensionLoader(root).scan(strict=True).register(
                registry, provider="test", model="test", sandbox_enabled=False,
                services=ExtensionServices(workspace_root=root, state_backend=Backend()),
                binding=ExtensionRuntimeBinding(trace_id="trace"),
            )
            original = [{"role": "user", "content": "original"}]
            event = HookEvent(
                point=HookPoint.MODEL_BEFORE, session_id="session",
                data={"messages": original, "tools": []},
            )
            asyncio.run(registry.emit(event))
            self.assertEqual(event.data["messages"], original)


class ExtensionRuntimeStateTests(unittest.TestCase):
    @staticmethod
    def _snapshot(source_hash: str) -> ExtensionTraceSnapshot:
        return ExtensionTraceSnapshot(
            trace_id="trace", hook_id="extension:turn_start:test",
            stage=HookPoint.TURN_START, source_hash=source_hash,
            manifest_hash=hashlib.sha256(b"manifest").hexdigest(),
            grant_version=1,
            requested_capabilities=(ExtensionCapability.LOGGER_WRITE,),
            effective_capabilities=(ExtensionCapability.LOGGER_WRITE,),
        )

    def test_policy_denial_is_separate_and_quarantine_is_source_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = GatewayStore(root / ".yy" / "gateway")
            controller = StateController(store.database_path, gateway_epoch="test")
            source_a = "a" * 64
            source_b = "b" * 64
            snapshot_a = self._snapshot(source_a)
            controller.record_extension_hook_outcome(
                snapshot_a, classification="exception", duration=0.1,
                error=RuntimeError("one"), run_id=None,
            )
            controller.record_extension_hook_outcome(
                snapshot_a, classification="policy_denial", duration=0.1,
                error=PermissionError("denied"), run_id=None,
            )
            controller.record_extension_hook_outcome(
                snapshot_a, classification="exception", duration=0.1,
                error=RuntimeError("two"), run_id=None,
            )
            state = controller.extension_status(
                hook_id=snapshot_a.hook_id, stage=snapshot_a.stage.value,
                source_hash=source_a,
            )[0]
            self.assertEqual(state["runtime_failure_streak"], 2)
            self.assertEqual(state["policy_denial_count"], 1)
            self.assertEqual(state["status"], "active")
            controller.record_extension_hook_outcome(
                snapshot_a, classification="exception", duration=0.1,
                error=RuntimeError("three"), run_id=None,
            )
            self.assertTrue(controller.extension_hook_is_quarantined(
                snapshot_a.hook_id, snapshot_a.stage.value, source_a,
            ))
            self.assertFalse(controller.extension_hook_is_quarantined(
                snapshot_a.hook_id, snapshot_a.stage.value, source_b,
            ))

    def test_success_resets_only_runtime_failure_streak(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = GatewayStore(root / ".yy" / "gateway")
            controller = StateController(store.database_path, gateway_epoch="test")
            snapshot = self._snapshot("c" * 64)
            controller.record_extension_hook_outcome(
                snapshot, classification="exception", duration=0.1,
                error=RuntimeError("one"), run_id=None,
            )
            controller.record_extension_hook_outcome(
                snapshot, classification="success", duration=0.1,
                error=None, run_id=None,
            )
            state = controller.extension_status(
                hook_id=snapshot.hook_id, stage=snapshot.stage.value,
                source_hash=snapshot.source_hash,
            )[0]
            self.assertEqual(state["runtime_failure_count"], 1)
            self.assertEqual(state["runtime_failure_streak"], 0)

    def test_grant_snapshot_is_frozen_for_trace_and_new_trace_sees_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(
                root, "turn_start", "snapshot.py", "snapshot", 0,
                "context.log('allowed')", capabilities=("logger.write",),
            )
            catalog = ExtensionLoader(root).scan(strict=True)

            class Backend:
                grant = True

                def __init__(self):
                    self.logs: list[str] = []
                    self.outcomes: list[str] = []

                def resolve_extension_grant(self, *args):
                    del args
                    if not self.grant:
                        return None
                    return {
                        "grant_version": 1,
                        "granted_capabilities": ["logger.write"],
                        "granted_tools": [], "tool_contract_hashes": {},
                    }

                def extension_hook_is_quarantined(self, *args):
                    del args
                    return False

                def record_extension_audit(self, snapshot, **kwargs):
                    del snapshot
                    self.logs.append(kwargs["result"])

                def record_extension_hook_outcome(self, snapshot, **kwargs):
                    del snapshot
                    self.outcomes.append(kwargs["classification"])

            backend = Backend()
            old_trace = HookRegistry()
            catalog.register(
                old_trace, provider="test", model="test", sandbox_enabled=False,
                services=ExtensionServices(workspace_root=root, state_backend=backend),
                binding=ExtensionRuntimeBinding(trace_id="old"),
            )
            backend.grant = False
            event = HookEvent(point=HookPoint.TURN_START, session_id="session", data={})
            asyncio.run(old_trace.emit(event))
            self.assertEqual(backend.logs, ["allowed"])

            new_trace = HookRegistry()
            catalog.register(
                new_trace, provider="test", model="test", sandbox_enabled=False,
                services=ExtensionServices(workspace_root=root, state_backend=backend),
                binding=ExtensionRuntimeBinding(trace_id="new"),
            )
            asyncio.run(new_trace.emit(event))
            self.assertEqual(backend.outcomes[-1], "policy_denial")

    def test_grant_intent_is_not_active_until_committed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            controller = StateController(store.database_path, gateway_epoch="test")
            run_id = uuid4().hex
            controller.create_run(
                run_id=run_id, workload_kind=WorkloadKind.CODE_FINALIZE,
                project_id=project.project_id, client_id="code-user", task="finalize",
                idempotency_key=uuid4().hex,
                request_hash=hashlib.sha256(b"finalize").hexdigest(),
            )
            source_hash = "d" * 64
            manifest_hash = "e" * 64
            plan = {
                "schema_version": 1,
                "candidate_commit": "f" * 40,
                "hooks": [{
                    "hook_id": "extension:turn_start:test",
                    "stage": "turn_start",
                    "source_hash": source_hash,
                    "manifest_hash": manifest_hash,
                    "auto_granted_capabilities": ["logger.write"],
                    "confirmation_required_capabilities": [],
                    "tools": [],
                }],
            }
            plan_hash = hashlib.sha256(b"plan").hexdigest()
            controller.begin_extension_grant_intent(
                plan_hash=plan_hash, candidate_commit="f" * 40,
                repository_identity="repo", plan=plan, actor="tester", run_id=run_id,
            )
            self.assertIsNone(controller.resolve_extension_grant(
                "extension:turn_start:test", "turn_start", source_hash, manifest_hash,
            ))
            controller.commit_extension_grant_intent(plan_hash)
            grant = controller.resolve_extension_grant(
                "extension:turn_start:test", "turn_start", source_hash, manifest_hash,
            )
            self.assertIsNotNone(grant)
            self.assertEqual(grant["granted_capabilities"], ["logger.write"])
            self.assertEqual(controller.events(run_id)[-1].type, "extension_grant_committed")

    def test_run_bound_hook_audit_enqueues_gateway_event_in_same_store(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = GatewayStore(root / ".yy" / "gateway")
            project = store.register_project(root)
            controller = StateController(store.database_path, gateway_epoch="test")
            run_id = uuid4().hex
            controller.create_run(
                run_id=run_id, workload_kind=WorkloadKind.CHAT,
                project_id=project.project_id, client_id="client", task="task",
                idempotency_key=uuid4().hex,
                request_hash=hashlib.sha256(b"task").hexdigest(),
            )
            snapshot = self._snapshot("f" * 64)
            controller.record_extension_hook_outcome(
                snapshot, classification="success", duration=0.01,
                error=None, run_id=run_id,
            )
            self.assertEqual(controller.events(run_id)[-1].type, "extension_hook_audit")
            with controller._connection() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS value FROM event_outbox WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            self.assertGreaterEqual(int(row["value"]), 2)


class CodingSourceConfigTests(unittest.TestCase):
    def test_migration_marker_supplies_coding_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as home_folder, tempfile.TemporaryDirectory() as source_folder:
            home, source = Path(home_folder), Path(source_folder)
            marker = home / ".yy" / "agent-home-migration.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(
                '{"source_root": ' + repr(str(source)).replace("'", '"').replace("\\", "\\\\") + "}",
                encoding="utf-8",
            )
            config = load_runtime_config(home, workspace_root=source)
            self.assertEqual(config.coding_source_root, source.resolve())


class _FakeCodingRuntime:
    def __init__(self, config, worktree: Path) -> None:
        self.config = config.model_copy(update={"workspace_root": worktree})
        self.tool_context = SimpleNamespace(project_root=worktree)
        self.coding_session_id = "coding-memory"
        self.worktree = worktree
        self.closed = False

    async def run_task(self, task: str, session_id: str):
        del session_id
        import re
        match = re.search(r"`(tests/extensions/test_[a-z0-9_]+\.py)`", task)
        if match:
            extension = self.worktree / "extension" / "hook" / "turn_start" / "added_capability.py"
            extension.parent.mkdir(parents=True, exist_ok=True)
            extension.write_text(
                "EXTENSION_NAME = 'added-capability'\nPRIORITY = 0\n\n"
                "EXTENSION_MANIFEST = {'schema_version': 1, 'capabilities': [], "
                "'allowed_tools': [], 'timeout_seconds': 5.0}\n\n"
                "async def handle(event, context):\n    return None\n",
                encoding="utf-8",
            )
            test = self.worktree / match.group(1)
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text("def test_added():\n    assert True\n", encoding="utf-8")
        yield RunEvent(type=EventType.FINAL, payload={"answer": "done"})

    async def close(self) -> None:
        self.closed = True


class CodeSessionControllerTests(unittest.TestCase):
    def test_worktree_is_under_agent_home_and_no_change_finalize_cleans(self) -> None:
        from run_ui.harness_loader import load_harness_module

        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as home_folder:
            source, home = Path(source_folder), Path(home_folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", "seed"],
                cwd=source, check=True, capture_output=True,
            )
            config = load_runtime_config(
                home,
                workspace_root=source,
                coding_source_root=source,
                provider="echo",
                model="echo",
            )
            controller = harness.CodeSessionController(
                config,
                runtime_factory=lambda selected, worktree: _FakeCodingRuntime(selected, worktree),
            )

            async def scenario():
                record = await controller.start()
                expected = home / ".yy" / "harness-evolution" / "worktrees"
                self.assertIn(expected.resolve(), record.worktree_path.parents)
                result = await controller.finalize()
                self.assertEqual(result.status, "no_changes")
                self.assertFalse(record.worktree_path.exists())

            asyncio.run(scenario())

    def test_verified_turn_commits_and_finalize_fast_forwards(self) -> None:
        from run_ui.harness_loader import load_harness_module

        harness = load_harness_module()

        class FastController(harness.CodeSessionController):
            async def _validate_and_test(self, record, test_file):
                del record, test_file
                return {"passed": True, "feedback": ""}

        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as home_folder:
            source, home = Path(source_folder), Path(home_folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", "seed"],
                cwd=source, check=True, capture_output=True,
            )
            config = load_runtime_config(
                home,
                workspace_root=source,
                coding_source_root=source,
                provider="echo",
                model="echo",
            )
            controller = FastController(
                config,
                runtime_factory=lambda selected, worktree: _FakeCodingRuntime(selected, worktree),
            )

            async def scenario():
                record = await controller.start()
                turn = await controller.run_turn("增加一项扩展能力")
                self.assertEqual(turn.status, "verified")
                self.assertNotEqual(turn.commit, record.base_commit)
                result = await controller.finalize()
                self.assertTrue(result.merged)
                self.assertTrue(
                    (source / "extension" / "hook" / "turn_start" / "added_capability.py").is_file()
                )

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
