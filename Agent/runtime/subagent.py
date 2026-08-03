"""无独立记忆的子 Agent 工具执行器。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from Agent.config import RuntimeConfig
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from prompt import compose_subagent_messages
from tool import AsyncToolRegistry, ToolContext


class _NoMemory:
    """临时 Subagent Runtime 的无持久化占位对象。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def has_session(self, session_id: str) -> bool:
        return True

    def session_created_at(self, session_id: str) -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def active_path(self, session_id: str) -> Path:
        return self.root / ".yy" / "ephemeral-subagent.jsonl"

    def prompt_context(self, session_id: str | None = None) -> str:
        return ""

    def latest_summary(self, session_id: str) -> str:
        return ""


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
            event.data["tools"] = selected.schemas(context)

        hooks.register(HookPoint.MODEL_BEFORE, inject_prompt, priority=-100)
        config = self.config.model_copy(update={"stream": False, "compression_threshold_tokens": 0})
        provider = build_provider(
            config.provider,
            config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            stream=False,
            use_system_proxy=config.use_system_proxy,
            proxy_url=config.proxy_url,
        )
        runtime = AgentRuntime(
            config,
            provider=provider,
            tools=selected,
            memory=_NoMemory(config.agent_root),
            hooks=hooks,
            tool_context=context,
            enable_context_processing=False,
            enable_skills=False,
            enable_subagent=False,
            enable_sandbox=False,
            enable_extensions=False,
            enable_references=False,
        )
        result = await runtime.run(task)
        if not result.completed:
            raise RuntimeError("子 Agent 未能完成委派任务")
        return result.answer
