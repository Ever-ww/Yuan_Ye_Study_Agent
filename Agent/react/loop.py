"""单一异步 ReAct 循环；Turn 生命周期由 Runtime 的用户任务边界管理。"""

from __future__ import annotations

import asyncio
import copy
import inspect
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
    """在一个 Runtime Turn 内执行多次模型调用与工具调用。"""

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
        """重复模型调用，直到模型返回最终文本或达到调用上限。"""
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
                before = HookEvent(point=HookPoint.MODEL_BEFORE, session_id=session_id, data={
                    "task": task,
                    "messages": messages,
                    "tools": schemas,
                    "model": model,
                    "first_model_call": not context_loaded,
                })
                try:
                    await self.hooks.emit(before)
                    compression_operation = before.data.pop("compression_operation", None)
                    if callable(compression_operation):
                        yield RunEvent(
                            type=EventType.COMPRESSION_STARTED,
                            payload={"session_id": session_id},
                        )
                        try:
                            compression_result = await compression_operation()
                            compression = (
                                compression_result.payload()
                                if compression_result is not None
                                else None
                            )
                        except Exception as exc:
                            compression = {
                                "status": "fallback",
                                "session_id": session_id,
                                "message": (
                                    "请求前自动压缩异常，已保留原上下文继续执行："
                                    f"{str(exc) or type(exc).__name__}"
                                ),
                            }
                        if compression is not None:
                            kind = (
                                EventType.CONTEXT_COMPRESSED
                                if compression.get("status") == "compressed"
                                else EventType.COMPRESSION_FALLBACK
                            )
                            yield RunEvent(type=kind, payload=compression)
                    persist_user = before.data.pop("persist_current_user_operation", None)
                    if callable(persist_user):
                        persisted = persist_user()
                        if inspect.isawaitable(persisted):
                            await persisted
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
                    _attach_failure_context(exc, messages, schemas, model, retry_history)
                    raise

                started_at = time.perf_counter()
                streamed = False
                streamed_parts: list[str] = []
                try:
                    if getattr(self.provider, "streaming", False) and getattr(self.provider, "stream", None):
                        streamed = True
                        reasoning_parts: list[str] = []
                        calls: tuple[ToolCall, ...] = ()
                        usage = None
                        async for chunk in self.provider.stream(messages, schemas):
                            if chunk.text:
                                streamed_parts.append(chunk.text)
                                yield RunEvent(type=EventType.TEXT, payload={"content": chunk.text})
                            if chunk.reasoning:
                                reasoning_parts.append(chunk.reasoning)
                            if chunk.tool_calls:
                                calls = chunk.tool_calls
                            if chunk.usage is not None:
                                usage = chunk.usage
                        reply = ModelReply(text="".join(streamed_parts), tool_calls=calls, finished=True, usage=usage, reasoning="".join(reasoning_parts) or None)
                    else:
                        reply = await self.provider.complete(messages, schemas)
                except asyncio.CancelledError as exc:
                    failure = {
                        "task": task,
                        "model": model,
                        "error": exc,
                        "completed": False,
                        "cancelled": True,
                        # 只保留已经展示给用户的自然语言；流式阶段未完成的
                        # tool_call 增量不具备可执行完整性，绝不能写入上下文。
                        "partial_text": "".join(streamed_parts),
                        "model_calls": model_calls,
                        "retry_history": list(retry_history),
                    }
                    await self.hooks.emit(HookEvent(
                        point=HookPoint.MODEL_AFTER,
                        session_id=session_id,
                        data=failure,
                    ))
                    _attach_failure_context(exc, messages, schemas, model, retry_history)
                    raise
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
                if retry_history:
                    yield RunEvent(type=EventType.MODEL_RECONNECTED, payload={
                        "attempt": attempt,
                        "recovered_failures": len(retry_history),
                        "message": "模型网络连接已恢复，继续当前任务",
                    })
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
                    _attach_failure_context(exc, messages, schemas, model, [])
                    raise
                successful_steps += 1
                continue

            yield RunEvent(type=EventType.FINAL, payload={
                "answer": reply.text,
                "completed": True,
                "model_calls": model_calls,
                "task_latency_ms": round((time.perf_counter() - task_started_at) * 1000, 2),
                "reasoning": reply.reasoning,
            })
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
            arguments = self.tools.prepare_arguments(name, arguments)
            yield RunEvent(type=EventType.TOOL_REQUESTED, payload={"name": name, "arguments": arguments})
            await self.hooks.emit(HookEvent(point=HookPoint.TOOL_DURING, session_id=session_id, data={
                "task": task, "name": name, "arguments": arguments, "tool_call_id": call_id,
            }))
            try:
                result = await self.tools.execute(name, arguments, context)
            except asyncio.CancelledError as exc:
                await self.hooks.emit(HookEvent(point=HookPoint.TOOL_AFTER, session_id=session_id, data={
                    "task": task,
                    "name": name,
                    "arguments": arguments,
                    "tool_call_id": call_id,
                    "result": None,
                    "error": exc,
                    "cancelled": True,
                }))
                raise
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
    return ModelReply(text=reply.text, tool_calls=calls, finished=reply.finished, usage=reply.usage, reasoning=reply.reasoning)


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
