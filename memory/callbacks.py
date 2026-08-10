"""把记忆能力注册为普通 Hook 回调函数，不定义专用 Hook 类型。"""

from __future__ import annotations

import json
from typing import Literal

from Agent.contracts import ModelReply
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models.errors import is_retryable_model_error
from memory.store import MemoryStore
from prompt import PromptComposer


def register_memory_callbacks(
    registry: HookRegistry,
    memory: MemoryStore,
    prompts: PromptComposer | None = None,
    *,
    session_origin: Literal["interactive", "cron", "maintenance"] = "interactive",
) -> None:
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
            current_user_message = dict(messages[-1])
        else:
            current_user_message = None
        base_system = base_systems.get(event.session_id)
        if base_system is None:
            base_system = dict(messages[0])
            base_systems[event.session_id] = base_system
        task = str(event.data.get("task", "")) if first_model_call else ""

        def rebuild_messages(*, refresh_system: bool = False) -> list[dict[str, object]]:
            nonlocal base_system
            if refresh_system and prompts is not None:
                base_system = {"role": "system", "content": prompts.refresh(event.session_id).content}
                base_systems[event.session_id] = dict(base_system)
            system = dict(base_system)
            rebuilt: list[dict[str, object]] = [
                system,
                *memory.restore_messages(event.session_id),
            ]
            if first_model_call:
                rebuilt.append(current_user_message or {"role": "user", "content": task})
            return rebuilt

        if first_model_call:
            messages[:] = rebuild_messages()
            event.data["persist_current_user_operation"] = (
                lambda: memory.record_user(
                    event.session_id,
                    task,
                    origin=session_origin,
                    audit=_audit(event),
                )
            )
        event.data["reload_messages_after_compression"] = lambda: rebuild_messages(refresh_system=True)

    async def clear_context_state(event: HookEvent) -> None:
        base_systems.pop(event.session_id, None)

    async def persist_answer(event: HookEvent) -> None:
        if event.data.get("cancelled"):
            record_id = memory.record_cancellation(event.session_id, audit=_audit(event))
            if record_id:
                event.data["session_record_id"] = record_id
            return
        error = event.data.get("error")
        if isinstance(error, BaseException) and is_retryable_model_error(error):
            record_id = memory.record_network_failure(event.session_id, audit=_audit(event))
            if record_id:
                event.data["session_record_id"] = record_id
            return
        if error is not None:
            record_id = memory.record_turn_failure(
                event.session_id,
                str(error) or type(error).__name__,
                audit=_audit(event),
            )
            if record_id:
                event.data["session_record_id"] = record_id
            return
        if not event.data.get("completed"):
            return
        answer = str(event.data.get("answer", ""))
        if not answer:
            return
        event.data["session_record_id"] = memory.record_assistant(
            event.session_id,
            answer,
            model=dict(event.data.get("model", {})),
            model_calls=list(event.data.get("model_calls", [])),
            task_latency_ms=float(event.data.get("task_latency_ms", 0.0)),
            reasoning=str(event.data["reasoning"]) if isinstance(event.data.get("reasoning"), str) else None,
            audit=_audit(event),
        )

    async def persist_model_tool_calls(event: HookEvent) -> None:
        """把每次模型返回的工具调用作为标准 assistant 消息落盘。"""
        if event.data.get("cancelled"):
            partial_text = event.data.get("partial_text")
            if isinstance(partial_text, str) and partial_text:
                event.data["session_record_id"] = memory.record_cancelled_partial(
                    event.session_id, partial_text, audit=_audit(event),
                )
            return
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
        event.data["session_record_id"] = memory.record_model_tool_calls(
            event.session_id,
            content=reply.text or None,
            tool_calls=calls,
            model=dict(event.data.get("model", {})),
            model_call=dict(event.data.get("model_call", {})),
            reasoning=reply.reasoning,
            audit=_audit(event),
        )

    async def persist_tool_result(event: HookEvent) -> None:
        """把工具成功结果或异常写为与 assistant.tool_calls 对应的 tool 消息。"""
        error = event.data.get("error")
        result = event.data.get("result")
        cancelled = bool(event.data.get("cancelled"))
        content = (
            "工具执行已由用户按 Ctrl+C 终止"
            if cancelled
            else str(result) if error is None
            else f"工具执行失败：{str(error) or type(error).__name__}"
        )
        event.data["session_record_id"] = memory.record_tool_result(
            event.session_id,
            tool_call_id=str(event.data.get("tool_call_id", "")),
            name=str(event.data.get("name", "")),
            content=content,
            status="cancelled" if cancelled else "success" if error is None else "error",
            arguments=dict(event.data.get("arguments", {})),
            audit=_audit(event),
        )

    async def prepare_history(event: HookEvent) -> None:
        """当前用户任务开始前只裁剪此前任务的工具输出投影。"""
        config = event.data.get("config")
        if config is None:
            return
        memory.prepare_historical_tool_outputs(
            event.session_id,
            max_chars=int(getattr(config, "tool_output_max_chars", 0)),
            head_ratio=float(getattr(config, "tool_output_head_ratio", 0.20)),
            tail_ratio=float(getattr(config, "tool_output_tail_ratio", 0.20)),
        )

    registry.register(HookPoint.TRACE_START, create_or_restore_session, priority=-100)
    registry.register(HookPoint.TURN_START, prepare_history, priority=-100)
    registry.register(HookPoint.MODEL_BEFORE, load_context, priority=-100)
    registry.register(HookPoint.MODEL_AFTER, persist_model_tool_calls, priority=100)
    registry.register(HookPoint.TOOL_AFTER, persist_tool_result, priority=100)
    registry.register(HookPoint.TURN_END, persist_answer, priority=100)
    registry.register(HookPoint.TRACE_END, clear_context_state, priority=100)


def _audit(event: HookEvent) -> dict[str, object]:
    value = event.data.get("durable_audit")
    if not isinstance(value, dict):
        return {}
    return {
        key: selected
        for key in ("run_id", "turn_id", "operation_id")
        if isinstance((selected := value.get(key)), str) and selected
    }
