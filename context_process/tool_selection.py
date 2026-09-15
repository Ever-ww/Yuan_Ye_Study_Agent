"""MODEL_BEFORE policy for turns that can only need a direct reply."""

from __future__ import annotations

import re

from Agent.hook import HookEvent, HookPoint, HookRegistry


_DIRECT_CONVERSATION_TASKS = frozenset({
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "hello",
    "hi",
    "hey",
    "你是谁",
    "你叫什么",
    "你叫什么名字",
    "介绍你自己",
    "介绍一下你自己",
    "介绍下你自己",
    "自我介绍",
    "你能做什么",
    "你有什么能力",
})


def is_direct_conversation_task(task: str) -> bool:
    """Return true only for a complete, unambiguous conversational utterance.

    This deliberately is not a general intent classifier.  Exact matching keeps
    compound requests such as ``介绍你自己并读取 README`` fully tool-capable.
    """

    normalized = re.sub(r"[\s，。！？、,.!?；;：:]", "", task).casefold()
    return normalized in _DIRECT_CONVERSATION_TASKS


def register_direct_response_tool_policy(registry: HookRegistry) -> None:
    """Hide Tool schemas for self-contained conversational turns.

    The policy changes only the current Provider projection.  It does not add a
    permission system, mutate the Runtime registry, or persist synthetic context.
    """

    async def prefer_direct_response(event: HookEvent) -> None:
        if not bool(event.data.get("first_model_call", False)):
            return
        task = event.data.get("task")
        tools = event.data.get("tools")
        if not isinstance(task, str) or not isinstance(tools, list):
            return
        if not is_direct_conversation_task(task):
            return
        event.data["tools"] = []
        event.data["tool_selection_policy"] = "direct_conversation"

    # Memory has rebuilt canonical history at -100.  Tool selection is decided
    # before trimming and budget forecasting so both observe the real request.
    registry.register(
        HookPoint.MODEL_BEFORE,
        prefer_direct_response,
        priority=-80,
        identity="direct_conversation_tool_policy",
    )


__all__ = [
    "is_direct_conversation_task",
    "register_direct_response_tool_policy",
]
