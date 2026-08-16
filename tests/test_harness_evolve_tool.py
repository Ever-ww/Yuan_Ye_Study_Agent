from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from gateway.durable_execution import _CURRENT_OPERATION
from tool import AsyncToolRegistry, ToolContext
from tools import HarnessEvolveTool


class _Service:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def evolve_capability(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "merged", "operation_id": kwargs["operation_id"]}

    async def reconcile_capability(self, operation_id: str):
        return {"status": "UNKNOWN", "operation_id": operation_id}


class HarnessEvolveToolTests(unittest.TestCase):
    def test_requires_durable_operation_and_is_not_delegable(self) -> None:
        service = _Service()
        tool = HarnessEvolveTool(service)
        context = ToolContext(project_root=Path.cwd())

        async def scenario() -> None:
            with self.assertRaisesRegex(RuntimeError, "durable Tool operation"):
                await tool.run({"task": "x", "capability_gap": {
                    "summary": "missing", "desired_behavior": "do x",
                    "current_limitation": "no tool", "acceptance_criteria": ["x works"],
                    "safety_constraints": [],
                }}, context)
            token = _CURRENT_OPERATION.set("op-1")
            try:
                result = json.loads(await tool.run({"task": "x", "capability_gap": {
                    "summary": "missing", "desired_behavior": "do x",
                    "current_limitation": "no tool", "acceptance_criteria": ["x works"],
                    "safety_constraints": [],
                }}, context))
            finally:
                _CURRENT_OPERATION.reset(token)
            self.assertEqual(result["operation_id"], "op-1")

        asyncio.run(scenario())
        self.assertEqual(service.calls[0]["operation_id"], "op-1")
        self.assertNotIn("target", service.calls[0])
        self.assertEqual(tool.runtime_profiles, ("interactive",))
        self.assertFalse(tool.delegatable)
        self.assertTrue(tool.ends_turn(json.dumps({"status": "merged", "restart_required": True})))
        self.assertFalse(tool.ends_turn(json.dumps({"status": "merged", "restart_required": False})))
        registry = AsyncToolRegistry([tool])
        with self.assertRaises(ValueError):
            registry.select(["harness_evolve"])
