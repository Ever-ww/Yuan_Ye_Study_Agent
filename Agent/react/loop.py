"""单一异步 ReAct 循环；Turn 只表示相邻模型调用之间的生命周期。"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from Agent.contracts import EventType, ModelProvider, ModelReply, RunEvent, ToolCall
from Agent.errors import AgentExecutionLimitError, AgentInvariantError
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models.errors import is_retryable_model_error
from Agent.retry import ModelRetryPolicy
from tools import AsyncToolRegistry, ToolContext


class ReactLoop:
    """一次模型调用及其请求的全部工具构成一个无编号 Turn。"""

    def __init__(
        self,
        provider: ModelProvider,
        tools: AsyncToolRegistry,
        hooks: HookRegistry,
        max_steps: int,
        retry_policy: ModelRetryPolicy | None = None,
    ) -> None:
        self.provider, self.tools, self.hooks, self.max_steps = provider, tools, hooks, max_steps
        self.retry_policy = retry_policy or ModelRetryPolicy(max_attempts=1, delay_seconds=0)

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

        successful_steps = 0
        context_loaded = False
        while successful_steps < self.max_steps:
            attempt = 0
            retry_history: list[dict[str, Any]] = []
            while True:
                attempt += 1
                schemas = self.tools.schemas()
                await self.hooks.emit(HookEvent(point=HookPoint.TURN_START, session_id=session_id, data={"task": task, "messages": messages}))
                before = HookEvent(point=HookPoint.MODEL_BEFORE, session_id=session_id, data={
                    "task": task,
                    "messages": messages,
                    "tools": schemas,
                    "model": model,
                    "first_model_call": not context_loaded,
                })
                try:
                    await self.hooks.emit(before)
                    messages = before.data.get("messages")
                    schemas = before.data.get("tools")
                    if not isinstance(messages, list) or not isinstance(schemas, list):
                        raise AgentInvariantError("model_before 必须保留列表形式的 messages 和 tools")
                    context_loaded = True
                    estimated_context = _estimate_tokens(json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False))
                    await self.hooks.emit(HookEvent(point=HookPoint.MODEL_DURING, session_id=session_id, data={
                        "task": task, "messages": messages, "tools": schemas, "model": model,
                    }))
                except Exception as exc:
                    await self._end_failed_turn(session_id, task, model, model_calls, exc)
                    _attach_failure_context(exc, messages, schemas, model, retry_history)
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
                                yield RunEvent(type=EventType.TEXT, payload={"content": chunk.text})
                            if chunk.tool_calls:
                                calls = chunk.tool_calls
                            if chunk.usage is not None:
                                usage = chunk.usage
                        reply = ModelReply(text="".join(parts), tool_calls=calls, finished=True, usage=usage)
                    else:
                        reply = await self.provider.complete(messages, schemas)
                except Exception as exc:
                    retry_history.append({
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "message": str(exc) or type(exc).__name__,
                    })
                    failure = {
                        "task": task,
                        "model": model,
                        "error": exc,
                        "completed": False,
                        "model_calls": model_calls,
                        "retry_history": list(retry_history),
                    }
                    try:
                        await self.hooks.emit(HookEvent(point=HookPoint.MODEL_AFTER, session_id=session_id, data=failure))
                        await self.hooks.emit(HookEvent(point=HookPoint.TURN_END, session_id=session_id, data=failure))
                    except Exception as hook_error:
                        _attach_failure_context(hook_error, messages, schemas, model, retry_history)
                        raise
                    if is_retryable_model_error(exc) and attempt < self.retry_policy.max_attempts:
                        yield RunEvent(type=EventType.MODEL_RETRY, payload={
                            "attempt": attempt + 1,
                            "max_attempts": self.retry_policy.max_attempts,
                            "delay_seconds": self.retry_policy.delay_seconds,
                            "message": str(exc) or type(exc).__name__,
                        })
                        await asyncio.sleep(self.retry_policy.delay_seconds)
                        continue
                    _attach_failure_context(exc, messages, schemas, model, retry_history)
                    raise
                break

            reply = _ensure_tool_call_ids(reply)
            call_metric = _model_call_metric(
                round((time.perf_counter() - started_at) * 1000, 2),
                estimated_context,
                question_tokens,
                reply,
            )
            model_calls.append(call_metric)
            after = HookEvent(point=HookPoint.MODEL_AFTER, session_id=session_id, data={
                "task": task, "model": model, "reply": reply, "error": None, "model_call": call_metric,
            })
            try:
                await self.hooks.emit(after)
            except Exception as exc:
                await self._end_failed_turn(session_id, task, model, model_calls, exc)
                _attach_failure_context(exc, messages, schemas, model, [])
                raise
            reply = after.data.get("reply")
            if not isinstance(reply, ModelReply):
                error = AgentInvariantError("model_after 必须保留 ModelReply 类型的 reply")
                _attach_failure_context(error, messages, schemas, model, [])
                raise error
            reply = _ensure_tool_call_ids(reply)
            if reply.text and not streamed:
                yield RunEvent(type=EventType.TEXT, payload={"content": reply.text})

            if reply.tool_calls:
                prepared_calls = [(call, str(call.id)) for call in reply.tool_calls]
                messages.append(_assistant_tool_message(reply))
                try:
                    async for event in self._execute_tools(prepared_calls, messages, context, task, session_id):
                        yield event
                except Exception as exc:
                    await self._end_failed_turn(session_id, task, model, model_calls, exc)
                    _attach_failure_context(exc, messages, schemas, model, [])
                    raise
                await self.hooks.emit(HookEvent(point=HookPoint.TURN_END, session_id=session_id, data={
                    "task": task, "model": model, "reply": reply, "error": None, "completed": False, "model_calls": model_calls,
                }))
                successful_steps += 1
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
            ended = await self.hooks.emit(HookEvent(point=HookPoint.TURN_END, session_id=session_id, data=completed))
            operation = ended.data.get("compression_operation")
            if callable(operation):
                yield RunEvent(type=EventType.COMPRESSION_STARTED, payload={"session_id": session_id})
                try:
                    result = await operation()
                    compression = result.payload()
                except Exception as exc:
                    compression = {
                        "status": "fallback",
                        "session_id": session_id,
                        "message": f"自动压缩失败，当前回答已保留：{str(exc) or type(exc).__name__}",
                    }
                kind = EventType.CONTEXT_COMPRESSED if compression.get("status") == "compressed" else EventType.COMPRESSION_FALLBACK
                yield RunEvent(type=kind, payload=compression)
            yield RunEvent(type=EventType.FINAL, payload={"answer": reply.text, "completed": True, "model_calls": model_calls})
            return

        error = AgentExecutionLimitError("模型在最大调用次数内未完成")
        _attach_failure_context(error, messages, self.tools.schemas(), model, [])
        raise error

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
            before = HookEvent(point=HookPoint.TOOL_BEFORE, session_id=session_id, data={
                "task": task, "name": call.name, "arguments": dict(call.arguments), "tool_call_id": call_id,
            })
            await self.hooks.emit(before)
            name, arguments = before.data.get("name"), before.data.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ValueError("tool_before 必须保留字符串 name 和对象 arguments")
            yield RunEvent(type=EventType.TOOL_REQUESTED, payload={"name": name, "arguments": arguments})
            await self.hooks.emit(HookEvent(point=HookPoint.TOOL_DURING, session_id=session_id, data={
                "task": task, "name": name, "arguments": arguments, "tool_call_id": call_id,
            }))
            try:
                result = await self.tools.execute(name, arguments, context)
            except Exception as exc:
                await self.hooks.emit(HookEvent(point=HookPoint.TOOL_AFTER, session_id=session_id, data={
                    "task": task, "name": name, "arguments": arguments, "tool_call_id": call_id, "result": None, "error": exc,
                }))
                raise
            after = HookEvent(point=HookPoint.TOOL_AFTER, session_id=session_id, data={
                "task": task, "name": name, "arguments": arguments, "tool_call_id": call_id, "result": result, "error": None,
            })
            await self.hooks.emit(after)
            result = str(after.data.get("result", result))
            yield RunEvent(type=EventType.TOOL_COMPLETED, payload={"name": name, "content": result})
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
        await self.hooks.emit(HookEvent(point=HookPoint.TURN_END, session_id=session_id, data={
            "task": task, "model": model, "error": error, "completed": False, "model_calls": model_calls,
        }))


def _assistant_tool_message(reply: ModelReply) -> dict[str, Any]:
    """构造可再次发送给 OpenAI-compatible 接口的 assistant 工具消息。"""
    serialized = [{
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
    } for call in reply.tool_calls]
    return {"role": "assistant", "content": reply.text or None, "tool_calls": serialized}


def _ensure_tool_call_ids(reply: ModelReply) -> ModelReply:
    """在任何 model_after 回调前为工具调用补齐稳定 ID。"""
    if not reply.tool_calls or all(call.id for call in reply.tool_calls):
        return reply
    calls = tuple(ToolCall(name=call.name, arguments=dict(call.arguments), id=call.id or f"call_{uuid4().hex}") for call in reply.tool_calls)
    return ModelReply(text=reply.text, tool_calls=calls, finished=reply.finished, usage=reply.usage)


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


def _attach_failure_context(
    error: BaseException,
    messages: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
    model: dict[str, Any],
    retry_history: list[dict[str, Any]],
) -> None:
    """把可复现请求现场附到异常；失败时仍保留原始异常。"""
    try:
        setattr(error, "yy_failure_context", {
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(schemas),
            "model": copy.deepcopy(model),
            "retry_history": copy.deepcopy(retry_history),
        })
    except Exception:
        return
