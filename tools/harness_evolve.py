"""One-release compatibility alias for :mod:`tools.harness_capability`."""

from __future__ import annotations

from typing import Any

from .harness_capability import HarnessCapabilityService, HarnessCapabilityTool


class HarnessEvolveTool(HarnessCapabilityTool):
    """Deprecated direct-call alias; new Runtime schemas advertise harness_capability."""

    name = "harness_evolve"
    description = "在用户高风险审批后，使用隔离 Harness worktree 修复 Yuan Ye 自身缺失或损坏的 Tool/Hook 能力。仅用于 Agent 自身能力缺口，不用于普通项目代码。"
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
    async def run(self, arguments: dict[str, Any], context) -> str:
        raw_gap = arguments.get("capability_gap")
        if not isinstance(raw_gap, dict):
            text = str(raw_gap or arguments.get("task") or "")
            arguments = {
                **arguments,
                "capability_gap": {
                    "summary": text,
                    "desired_behavior": str(arguments.get("task") or text),
                    "current_limitation": text,
                    "acceptance_criteria": [str(arguments.get("task") or text)],
                    "safety_constraints": [],
                },
            }
        return await super().run(arguments, context)


HarnessEvolutionService = HarnessCapabilityService

__all__ = ["HarnessEvolutionService", "HarnessEvolveTool"]
