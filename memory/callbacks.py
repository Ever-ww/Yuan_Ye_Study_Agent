"""把记忆能力注册为普通 Hook 回调函数，不定义专用 Hook 类型。"""

from __future__ import annotations

import json

from Agent.contracts import ModelReply
from Agent.hook import HookEvent, HookPoint, HookRegistry
from memory.store import MemoryStore


def register_memory_callbacks(registry: HookRegistry, memory: MemoryStore) -> None:
    """注册会话创建、上下文加载和最终回复持久化回调。"""
    base_systems: dict[str, dict[str, object]] = {}

    async def create_or_restore_session(event: HookEvent) -> None:
        if memory.has_session(event.session_id):
            return
        if not event.data.get("new_session"):
            raise KeyError(f"未知会话：{event.session_id}")
        memory.create_session(str(event.data.get("task", "")), session_id=event.session_id)

    async def load_context(event: HookEvent) -> None:
        messages = event.data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Memory 回调需要基础 system 消息")
        first_model_call = bool(event.data.get("first_model_call"))
        if first_model_call:
            if len(messages) < 2:
                raise ValueError("Memory 回调需要基础 system/user 消息")
            base_systems[event.session_id] = dict(messages[0])
        base_system = base_systems.get(event.session_id)
        if base_system is None:
            base_system = dict(messages[0])
            base_systems[event.session_id] = base_system
        task = str(event.data.get("task", "")) if first_model_call else ""

        def rebuild_messages() -> list[dict[str, object]]:
            system = dict(base_system)
            profile = memory.prompt_context(event.session_id)
            if profile:
                system["content"] = (
                    f"{system.get('content', '')}\n\n长期记忆与项目上下文：\n{profile}"
                )
            rebuilt: list[dict[str, object]] = [
                system,
                *memory.restore_messages(event.session_id),
            ]
            if first_model_call:
                rebuilt.append({"role": "user", "content": task})
            return rebuilt

        if first_model_call:
            messages[:] = rebuild_messages()
            event.data["persist_current_user_operation"] = (
                lambda: memory.record_user(event.session_id, task)
            )
        event.data["reload_messages_after_compression"] = rebuild_messages

    async def clear_context_state(event: HookEvent) -> None:
        base_systems.pop(event.session_id, None)

    async def persist_answer(event: HookEvent) -> None:
        if event.data.get("error") is not None or not event.data.get("completed"):
            return
        answer = str(event.data.get("answer", ""))
        if not answer:
            return
        memory.record_assistant(
            event.session_id,
            answer,
            model=dict(event.data.get("model", {})),
            model_calls=list(event.data.get("model_calls", [])),
            task_latency_ms=float(event.data.get("task_latency_ms", 0.0)),
        )

    async def persist_model_tool_calls(event: HookEvent) -> None:
        """把每次模型返回的工具调用作为标准 assistant 消息落盘。"""
        if event.data.get("error") is not None:
            return
        reply = event.data.get("reply")
        if not isinstance(reply, ModelReply) or not reply.tool_calls:
            return
        calls = [{
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
        } for call in reply.tool_calls]
        memory.record_model_tool_calls(
            event.session_id,
            content=reply.text or None,
            tool_calls=calls,
            model=dict(event.data.get("model", {})),
            model_call=dict(event.data.get("model_call", {})),
        )

    async def persist_tool_result(event: HookEvent) -> None:
        """把工具成功结果或异常写为与 assistant.tool_calls 对应的 tool 消息。"""
        error = event.data.get("error")
        result = event.data.get("result")
        content = str(result) if error is None else f"工具执行失败：{str(error) or type(error).__name__}"
        memory.record_tool_result(
            event.session_id,
            tool_call_id=str(event.data.get("tool_call_id", "")),
            name=str(event.data.get("name", "")),
            content=content,
            status="success" if error is None else "error",
            arguments=dict(event.data.get("arguments", {})),
        )

    registry.register(HookPoint.TRACE_START, create_or_restore_session, priority=-100)
    registry.register(HookPoint.MODEL_BEFORE, load_context, priority=-100)
    registry.register(HookPoint.MODEL_AFTER, persist_model_tool_calls, priority=100)
    registry.register(HookPoint.TOOL_AFTER, persist_tool_result, priority=100)
    registry.register(HookPoint.TURN_END, persist_answer, priority=100)
    registry.register(HookPoint.TRACE_END, clear_context_state, priority=100)
