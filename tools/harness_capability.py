"""Canonical model-visible CAPABILITY entry for Harness evolution."""

from __future__ import annotations

import json
from typing import Any, Protocol

from tool.contracts import ToolContext


class HarnessCapabilityService(Protocol):
    async def evolve_capability(
        self, *, operation_id: str, task: str, capability_gap: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def reconcile_capability(self, operation_id: str) -> dict[str, Any]: ...


class HarnessCapabilityTool:
    name = "harness_capability"
    description = (
        "在用户高风险审批后，通过隔离Harness worktree补充或修复Yuan Ye自身缺失的Tool能力；"
        "不用于普通项目代码、Hook修改、参数错误或临时网络故障。"
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "minLength": 1},
            "capability_gap": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "desired_behavior": {"type": "string", "minLength": 1},
                    "current_limitation": {"type": "string", "minLength": 1},
                    "acceptance_criteria": {
                        "type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1,
                    },
                    "safety_constraints": {
                        "type": "array", "items": {"type": "string", "minLength": 1},
                    },
                },
                "required": ["summary", "desired_behavior", "current_limitation", "acceptance_criteria"],
                "additionalProperties": False,
            },
        },
        "required": ["task", "capability_gap"],
        "additionalProperties": False,
    }
    risk = "high"
    idempotency = "NON_IDEMPOTENT"
    delegatable = False
    runtime_profiles = ("interactive",)

    def __init__(self, service: HarnessCapabilityService) -> None:
        self.service = service

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        from gateway.durable_execution import current_operation_id

        operation_id = current_operation_id()
        if not operation_id:
            raise RuntimeError("harness_capability must run inside a durable Tool operation")
        result = await self.service.evolve_capability(
            operation_id=operation_id,
            task=str(arguments["task"]),
            capability_gap=dict(arguments["capability_gap"]),
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def ends_turn(result: str) -> bool:
        try:
            value = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return False
        return bool(value.get("restart_required")) and value.get("status") == "merged"

    async def reconcile(self, operation: Any, context: ToolContext) -> dict[str, Any]:
        del context
        return await self.service.reconcile_capability(str(operation.operation_id))


__all__ = ["HarnessCapabilityService", "HarnessCapabilityTool"]
