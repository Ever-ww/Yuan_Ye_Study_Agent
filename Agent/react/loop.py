"""模型与工具之间的单一异步 ReAct 执行循环。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from Agent.contracts import EventType, ModelProvider, RunEvent
from Agent.extensions import HookRegistry
from tools import AsyncToolRegistry, ToolContext


class ReactLoop:
    """执行模型输出、工具调用和 Observation 回填。"""

    def __init__(self, provider: ModelProvider, tools: AsyncToolRegistry, hooks: HookRegistry, max_steps: int) -> None:
        self.provider, self.tools, self.hooks, self.max_steps = provider, tools, hooks, max_steps

    async def run(self, messages: list[dict[str, Any]], context: ToolContext) -> AsyncIterator[RunEvent]:
        """持续运行到模型完成或达到明确步数上限。"""
        for _ in range(self.max_steps):
            answer_parts: list[str] = []
            tool_calls = ()
            stream = getattr(self.provider, "stream", None) if getattr(self.provider, "streaming", False) else None
            if stream:
                async for reply in stream(messages, self.tools.schemas()):
                    if reply.text:
                        answer_parts.append(reply.text)
                        yield RunEvent(EventType.TEXT, {"content": reply.text})
                    if reply.tool_calls:
                        tool_calls = reply.tool_calls
            else:
                reply = await self.provider.complete(messages, self.tools.schemas())
                answer_parts.append(reply.text)
                tool_calls = reply.tool_calls
                if reply.text:
                    yield RunEvent(EventType.TEXT, {"content": reply.text})
            answer = "".join(answer_parts)
            if not tool_calls:
                yield RunEvent(EventType.FINAL, {"answer": answer, "completed": True})
                return
            for call in tool_calls:
                yield RunEvent(EventType.TOOL_REQUESTED, {"name": call.name, "arguments": call.arguments})
                arguments = await self.hooks.before_tool(call.name, call.arguments)
                result = await self.tools.execute(call.name, arguments, context)
                yield RunEvent(EventType.TOOL_COMPLETED, {"name": call.name, "content": result})
                messages.append({"role": "tool", "name": call.name, "content": result})
        yield RunEvent(EventType.ERROR, {"message": "模型在最大步骤数内未完成"})
