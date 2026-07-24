"""由父 Agent 显式限制能力的子 Agent 工具。"""

from __future__ import annotations

from typing import Any, Protocol

from .contracts import ToolContext, ToolRisk


class SubagentRunner(Protocol):
    """Runtime 注入的无持久化子 Agent 执行器。"""

    async def __call__(
        self,
        task: str,
        instructions: str,
        tools: list[str],
        context: ToolContext,
    ) -> str: ...


class SubagentTool:
    """把独立模型任务委派为父 Agent 的一次普通工具调用。"""

    name = "subagent"
    description = "启动无独立记忆的子 Agent 完成任务，并返回最终结果"
    risk = "dynamic"

    def __init__(self, runner: SubagentRunner, available_risks: dict[str, ToolRisk]) -> None:
        self.runner = runner
        self.available_risks = dict(available_risks)
        self.schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "instructions": {"type": "string"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(self.available_risks)},
                },
            },
            "required": ["task"],
        }

    def risk_for(self, arguments: dict[str, Any]) -> ToolRisk:
        """按委派工具子集计算风险，供统一 Registry 权限链使用。"""
        risks = [self.available_risks[name] for name in arguments.get("tools", [])]
        if "high" in risks or "dynamic" in risks:
            return "high"
        if "write" in risks:
            return "write"
        return "read"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        names = list(arguments.get("tools", []))
        if "subagent" in names:
            raise ValueError("子 Agent 不允许递归调用 subagent")
        return await self.runner(
            arguments["task"],
            arguments.get("instructions", ""),
            names,
            context,
        )
