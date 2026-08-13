"""由父 Agent 显式限制能力的子 Agent 工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from tool.contracts import ToolContext, ToolRisk

if TYPE_CHECKING:
    from tool.registry import AsyncToolRegistry


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

    def __init__(
        self,
        runner: SubagentRunner,
        available_risks: dict[str, ToolRisk],
        registry: "AsyncToolRegistry",
    ) -> None:
        self.runner = runner
        self.available_risks = dict(available_risks)
        self.registry = registry
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

    def schema_for(self, context: ToolContext | None) -> dict[str, Any]:
        """只向父模型列出子 Agent 在当前沙箱状态下可委派的工具。"""
        allowed = sorted(
            name for name in self.registry.names(context)
            if name not in {"subagent", "skill_install", "cronjob", "harness_evolve"}
        )
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "instructions": {"type": "string"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string", "enum": allowed},
                },
            },
            "required": ["task"],
        }

    def ensure_available(self, arguments: dict[str, Any], context: ToolContext) -> None:
        """在父级审批前拒绝不可用能力，尤其禁止 checkpoint-only 委派 Bash。"""
        for name in arguments.get("tools", []):
            if name in {"subagent", "skill_install", "cronjob", "harness_evolve"}:
                raise ValueError(f"子 Agent 不允许选择工具：{name}")
            if not self.registry.is_available(name, context):
                if name == "bash":
                    from sandbox import BashUnavailableError
                    raise BashUnavailableError(
                        "当前 Trace 处于 checkpoint-only 模式，不能向子 Agent 委派 Bash",
                    )
                raise RuntimeError(f"子 Agent 工具当前不可用：{name}")

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
