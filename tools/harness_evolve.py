"""High-risk, non-delegable entry point for capability-driven Harness evolution."""

from __future__ import annotations

from typing import Any, Protocol

from tool.contracts import ToolContext


class HarnessEvolutionService(Protocol):
    async def evolve_capability(
        self, *, operation_id: str, task: str, target: str, capability_gap: str,
    ) -> dict[str, Any]: ...

    async def reconcile_capability(self, operation_id: str) -> dict[str, Any]: ...


class HarnessEvolveTool:
    name = "harness_evolve"
    description = "在用户高风险审批后，使用隔离 Harness worktree 修复 Yuan Ye 自身缺失或损坏的 Tool/Hook 能力。仅用于 Agent 自身能力缺口，不用于普通项目代码。"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "minLength": 1},
            "target": {"type": "string", "enum": ["extension", "tool"]},
            "capability_gap": {"type": "string", "minLength": 1},
        },
        "required": ["task", "target", "capability_gap"],
        "additionalProperties": False,
    }
    risk = "high"
    idempotency = "NON_IDEMPOTENT"

    def __init__(self, service: HarnessEvolutionService) -> None:
        self.service = service

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        # Import lazily so the generic tools package does not depend on Gateway at import time.
        from gateway.durable_execution import current_operation_id

        operation_id = current_operation_id()
        if not operation_id:
            raise RuntimeError("harness_evolve must run inside a durable Tool operation")
        result = await self.service.evolve_capability(
            operation_id=operation_id,
            task=str(arguments["task"]),
            target=str(arguments["target"]),
            capability_gap=str(arguments["capability_gap"]),
        )
        import json
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def reconcile(self, operation: Any, context: ToolContext) -> dict[str, Any]:
        del context
        return await self.service.reconcile_capability(str(operation.operation_id))
