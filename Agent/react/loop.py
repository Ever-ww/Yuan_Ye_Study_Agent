"""单一异步 ReAct 循环；Turn 只表示相邻模型调用之间的生命周期。"""

from __future__ import annotations

import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from Agent.contracts import EventType, ModelProvider, ModelReply, RunEvent, ToolCall
from Agent.hook import HookEvent, HookPoint, HookRegistry
from tools import AsyncToolRegistry, ToolContext


class ReactLoop:
    """一次模型调用及其请求的全部工具构成一个无编号 Turn。"""

    def __init__(self, provider: ModelProvider, tools: AsyncToolRegistry, hooks: HookRegistry, max_steps: int) -> None:
        self.provider, self.tools, self.hooks, self.max_steps = provider, tools, hooks, max_steps

    async def run(
        self,
        messages: list[dict[str, Any]],
        context: ToolContext,
        *,
        task: str,
        session_id: str,
        model: dict[str, Any],
    ) -> AsyncIterator[RunEvent]:
        """重复无编号 Turn，直到模型返回最终文本或达到调用上限。"""
        model_calls: list[dict[str, Any]] = []
        question_tokens = _estimate_tokens(task)
        task_started_at = time.perf_counter()

        for model_attempt in range(self.max_steps):
            await self.hooks.emit(HookEvent(HookPoint.TURN_START, session_id, {"task": task, "messages": messages}))
            schemas = self.tools.schemas()
            before = HookEvent(HookPoint.MODEL_BEFORE, session_id, {
                "task": task,
                "messages": messages,
                "tools": schemas,
                "model": model,
                "first_model_call": model_attempt == 0,
            })
            try:
                await self.hooks.emit(before)
                messages = before.data.get("messages")
                schemas = before.data.get("tools")
                if not isinstance(messages, list) or not isinstance(schemas, list):
                    raise ValueError("model_before 必须保留列表形式的 messages 和 tools")
                estimated_context = _estimate_tokens(json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False))
                await self.hooks.emit(HookEvent(HookPoint.MODEL_DURING, session_id, {
                    "task": task, "messages": messages, "tools": schemas, "model": model,
                }))
            except Exception as exc:
                await self._end_failed_turn(session_id, task, model, model_calls, exc)
                raise

            started_at = time.perf_counter()
            streamed = False
            try:
                if getattr(self.provider, "streaming", False) and getattr(self.provider, "stream", None):
                    streamed = True
                    parts: list[str] = []
                    calls: tuple[ToolCall, ...] = ()
                    usage = None
                    async for chunk in self.provider.stream(messages, schemas):
                        if chunk.text:
                            parts.append(chunk.text)
                            yield RunEvent(EventType.TEXT, {"content": chunk.text})
                        if chunk.tool_calls:
                            calls = chunk.tool_calls
                        if chunk.usage is not None:
                            usage = chunk.usage
                    reply = ModelReply("".join(parts), calls, True, usage)
                else:
                    reply = await self.provider.complete(messages, schemas)
            except Exception as exc:
                failure = {"task": task, "model": model, "error": exc, "completed": False, "model_calls": model_calls}
                await self.hooks.emit(HookEvent(HookPoint.MODEL_AFTER, session_id, failure))
                await self.hooks.emit(HookEvent(HookPoint.TURN_END, session_id, failure))
                raise

            call_metric = _model_call_metric(
                round((time.perf_counter() - started_at) * 1000, 2),
                estimated_context,
                question_tokens,
                reply,
            )
            model_calls.append(call_metric)
            after = HookEvent(HookPoint.MODEL_AFTER, session_id, {
                "task": task, "model": model, "reply": reply, "error": None, "model_call": call_metric,
            })
            try:
                await self.hooks.emit(after)
            except Exception as exc:
                await self._end_failed_turn(session_id, task, model, model_calls, exc)
                raise
            reply = after.data.get("reply")
            if not isinstance(reply, ModelReply):
                raise ValueError("model_after 必须保留 ModelReply 类型的 reply")
            if reply.text and not streamed:
                yield RunEvent(EventType.TEXT, {"content": reply.text})

            if reply.tool_calls:
                prepared_calls = [(call, call.id or f"call_{uuid4().hex}") for call in reply.tool_calls]
                messages.append(_assistant_tool_message(reply, prepared_calls))
                try:
                    async for event in self._execute_tools(prepared_calls, messages, context, task, session_id):
                        yield event
                except Exception as exc:
                    await self._end_failed_turn(session_id, task, model, model_calls, exc)
                    raise
                await self.hooks.emit(HookEvent(HookPoint.TURN_END, session_id, {
                    "task": task, "model": model, "reply": reply, "error": None, "completed": False, "model_calls": model_calls,
                }))
                continue

            completed = {
                "task": task,
                "model": model,
                "reply": reply,
                "answer": reply.text,
                "error": None,
                "completed": True,
                "model_calls": model_calls,
                "task_latency_ms": round((time.perf_counter() - task_started_at) * 1000, 2),
            }
            await self.hooks.emit(HookEvent(HookPoint.TURN_END, session_id, completed))
            yield RunEvent(EventType.FINAL, {"answer": reply.text, "completed": True, "model_calls": model_calls})
            return

        yield RunEvent(EventType.ERROR, {"message": "模型在最大调用次数内未完成"})

    async def _execute_tools(
        self,
        calls: list[tuple[ToolCall, str]],
        messages: list[dict[str, Any]],
        context: ToolContext,
        task: str,
        session_id: str,
    ) -> AsyncIterator[RunEvent]:
        """在当前无编号 Turn 内执行模型请求的全部工具。"""
        for call, call_id in calls:
            before = HookEvent(HookPoint.TOOL_BEFORE, session_id, {
                "task": task, "name": call.name, "arguments": dict(call.arguments), "tool_call_id": call_id,
            })
            await self.hooks.emit(before)
            name, arguments = before.data.get("name"), before.data.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ValueError("tool_before 必须保留字符串 name 和对象 arguments")
            yield RunEvent(EventType.TOOL_REQUESTED, {"name": name, "arguments": arguments})
            await self.hooks.emit(HookEvent(HookPoint.TOOL_DURING, session_id, {
                "task": task, "name": name, "arguments": arguments, "tool_call_id": call_id,
            }))
            try:
                result = await self.tools.execute(name, arguments, context)
            except Exception as exc:
                await self.hooks.emit(HookEvent(HookPoint.TOOL_AFTER, session_id, {
                    "task": task, "name": name, "arguments": arguments, "tool_call_id": call_id, "result": None, "error": exc,
                }))
                raise
            after = HookEvent(HookPoint.TOOL_AFTER, session_id, {
                "task": task, "name": name, "arguments": arguments, "tool_call_id": call_id, "result": result, "error": None,
            })
            await self.hooks.emit(after)
            result = str(after.data.get("result", result))
            yield RunEvent(EventType.TOOL_COMPLETED, {"name": name, "content": result})
            messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": result})

    async def _end_failed_turn(
        self,
        session_id: str,
        task: str,
        model: dict[str, Any],
        model_calls: list[dict[str, Any]],
        error: Exception,
    ) -> None:
        """确保已开始的 Turn 在失败时仍触发 turn_end。"""
        await self.hooks.emit(HookEvent(HookPoint.TURN_END, session_id, {
            "task": task, "model": model, "error": error, "completed": False, "model_calls": model_calls,
        }))


def _assistant_tool_message(reply: ModelReply, calls: list[tuple[ToolCall, str]]) -> dict[str, Any]:
    """构造可再次发送给 OpenAI-compatible 接口的 assistant 工具消息。"""
    serialized = [{
        "id": call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
    } for call, call_id in calls]
    return {"role": "assistant", "content": reply.text or None, "tool_calls": serialized}


def _model_call_metric(latency_ms: float, context_tokens: int, question_tokens: int, reply: ModelReply) -> dict[str, Any]:
    """生成一次无编号模型 API 调用的审计指标。"""
    serialized_calls = json.dumps([{"name": call.name, "arguments": call.arguments} for call in reply.tool_calls], ensure_ascii=False)
    usage = reply.usage
    return {
        "latency_ms": latency_ms,
        "input_tokens": {
            "context_total": usage.input_tokens if usage and usage.input_tokens is not None else context_tokens,
            "current_question": question_tokens,
            "context_source": "provider" if usage and usage.input_tokens is not None else "estimated",
            "current_question_source": "estimated",
        },
        "output_tokens": usage.output_tokens if usage and usage.output_tokens is not None else _estimate_tokens(reply.text + serialized_calls),
        "output_tokens_source": "provider" if usage and usage.output_tokens is not None else "estimated",
    }


def _estimate_tokens(value: str) -> int:
    """为 API 未提供的 Token 细分数据提供带来源标记的保守估算。"""
    if not value:
        return 0
    cjk = sum(1 for char in value if "\u3400" <= char <= "\u9fff")
    return cjk + math.ceil((len(value) - cjk) / 4)
