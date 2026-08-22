from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from Agent import RuntimeFailure, load_runtime_config
from Agent.state import WorkloadKind
from gateway.harness_evolution import GatewayHarnessEvolutionService
from gateway.state_controller import StateController
from gateway.store import GatewayStore
from tool import default_tools
from tools.harness_evolve import HarnessEvolveTool


ROOT = Path(__file__).resolve().parents[1]


class _InteractiveOnlyTool:
    name = "interactive_only_test"
    description = "profile isolation test"
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    risk = "read"
    runtime_profiles = ("interactive",)
    delegatable = False

    async def run(self, arguments, context):  # pragma: no cover - registry-only contract
        return "ok"


class HarnessArchitectureTests(unittest.TestCase):
    def test_facades_have_no_independent_repair_or_git_loop(self) -> None:
        tree = ast.parse((ROOT / "harness-evolution" / "harness.py").read_text(encoding="utf-8"))
        classes = {
            node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
        }
        for class_name, method_name in (
            ("CodeSessionController", "_run_turn_legacy"),
            ("HarnessEvolutionRunner", "_run_legacy"),
        ):
            method = next(
                node for node in classes[class_name].body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name
            )
            self.assertFalse(any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(method)))
            source = ast.unparse(method)
            self.assertIn("HarnessEvolutionEngine", source if class_name.endswith("Runner") else "HarnessEvolutionEngine" + source)
            self.assertNotIn("git merge", source)

    def test_capability_schema_is_structured_and_tool_only(self) -> None:
        schema = HarnessEvolveTool.schema
        self.assertNotIn("target", schema["properties"])
        gap = schema["properties"]["capability_gap"]
        self.assertEqual(gap["type"], "object")
        self.assertIn("acceptance_criteria", gap["required"])

    def test_runtime_profile_filter_excludes_interactive_only_tools(self) -> None:
        from tool import AsyncToolRegistry

        tool = _InteractiveOnlyTool()
        interactive = AsyncToolRegistry([tool])
        self.assertIn(tool.name, interactive.names())
        with self.assertRaises(ValueError):
            interactive.select([tool.name])
        # The default profile contract is observable and validated by candidate Harness.
        with tempfile.TemporaryDirectory() as value:
            registry = default_tools(Path(value), runtime_profile="cron")
            self.assertNotIn("harness_evolve", registry.names())

    def test_gateway_error_proposal_preserves_real_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            agent_root = Path(value)
            workspace = agent_root / "workspace"
            workspace.mkdir()
            config = load_runtime_config(
                agent_root, workspace_root=workspace, coding_source_root=ROOT,
            )
            store = GatewayStore(agent_root / ".yy" / "gateway")
            project = store.register_project(workspace)
            controller = StateController(store.database_path, gateway_epoch="test")
            run, _ = controller.create_run(
                run_id=uuid4().hex, workload_kind=WorkloadKind.CHAT,
                project_id=project.project_id, client_id="client", task="repair me",
                idempotency_key=uuid4().hex,
                request_hash=hashlib.sha256(b"repair me").hexdigest(),
            )
            error = RuntimeError("captured defect")
            error.yy_failure_context = {
                "messages": [{"role": "user", "content": "repair me"}],
                "tools": [{"name": "read_file", "parameters": {"type": "object"}}],
                "model": {"provider": "test", "model": "test-model"},
                "retry_history": [{"attempt": 1, "message": "first"}],
            }
            failure = RuntimeFailure.capture(error)
            service = GatewayHarnessEvolutionService(config, store=store)

            proposal = service.propose_error(run_id=run.run_id, failure=failure)

            self.assertIsNotNone(proposal)
            snapshot = Path(proposal["snapshot_path"])
            self.assertIn(agent_root / ".yy" / "harness-evolution", snapshot.parents)
            records = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines()]
            incident = next(item for item in records if item["record_type"] == "incident")
            self.assertEqual(incident["model"]["model"], "test-model")
            self.assertEqual(incident["retry_history"][0]["attempt"], 1)
            self.assertTrue(any(item.get("record_type") == "message" for item in records))
            self.assertTrue(any(item.get("record_type") == "tool_schema" for item in records))


if __name__ == "__main__":
    unittest.main()
