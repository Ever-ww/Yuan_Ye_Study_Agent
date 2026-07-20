"""无独立记忆的子 Agent 工具执行器。"""

from __future__ import annotations

from dataclasses import replace

from Agent.config import RuntimeConfig
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from prompt import compose_subagent_messages
from tools import AsyncToolRegistry, ToolContext


class RuntimeSubagentRunner:
    """使用父 Agent 的模型配置和显式工具子集执行临时任务。"""

    def __init__(self, config: RuntimeConfig, available_tools: AsyncToolRegistry) -> None:
        self.config = config
        self.available_tools = available_tools

    async def __call__(
        self,
        task: str,
        instructions: str,
        tools: list[str],
        context: ToolContext,
    ) -> str:
        from Agent.runtime.engine import AgentRuntime

        selected = self.available_tools.select(tools)
        hooks = HookRegistry()
        messages = compose_subagent_messages(task, instructions)

        async def inject_prompt(event: HookEvent) -> None:
            event.data["messages"] = [dict(message) for message in messages]
            event.data["tools"] = selected.schemas()

        hooks.register(HookPoint.MODEL_BEFORE, inject_prompt, priority=-100)
        config = replace(self.config, stream=False, compression_threshold_tokens=0)
        provider = build_provider(
            config.provider,
            config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            stream=False,
        )
        runtime = AgentRuntime(
            config,
            provider=provider,
            tools=selected,
            hooks=hooks,
            approval=context.approval,
            enable_context_processing=False,
            enable_subagent=False,
        )
        result = await runtime.run(task)
        if not result.completed:
            raise RuntimeError("子 Agent 未能完成委派任务")
        return result.answer
