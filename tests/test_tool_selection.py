from __future__ import annotations

import asyncio

from Agent.hook import HookEvent, HookPoint, HookRegistry
from context_process import (
    is_direct_conversation_task,
    register_direct_response_tool_policy,
)


def test_direct_conversation_matching_is_deliberately_narrow() -> None:
    assert is_direct_conversation_task("介绍一下你自己") is True
    assert is_direct_conversation_task(" 你是谁？ ") is True
    assert is_direct_conversation_task("hello!") is True
    assert is_direct_conversation_task("介绍你自己并读取 README") is False
    assert is_direct_conversation_task("你好，请搜索最新论文") is False


def test_model_before_hides_tools_only_for_first_direct_request() -> None:
    async def check() -> None:
        registry = HookRegistry()
        register_direct_response_tool_policy(registry)
        direct = HookEvent(
            point=HookPoint.MODEL_BEFORE,
            session_id="session",
            data={
                "task": "介绍一下你自己",
                "first_model_call": True,
                "tools": [{"name": "profile_read"}, {"name": "bash"}],
            },
        )
        await registry.emit(direct)
        assert direct.data["tools"] == []
        assert direct.data["tool_selection_policy"] == "direct_conversation"

        compound = HookEvent(
            point=HookPoint.MODEL_BEFORE,
            session_id="session",
            data={
                "task": "介绍你自己并读取 README",
                "first_model_call": True,
                "tools": [{"name": "read_file"}],
            },
        )
        await registry.emit(compound)
        assert compound.data["tools"] == [{"name": "read_file"}]

        later = HookEvent(
            point=HookPoint.MODEL_BEFORE,
            session_id="session",
            data={
                "task": "你好",
                "first_model_call": False,
                "tools": [{"name": "calculator"}],
            },
        )
        await registry.emit(later)
        assert later.data["tools"] == [{"name": "calculator"}]

    asyncio.run(check())
