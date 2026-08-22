"""单一异步 ReAct 循环；Turn 生命周期由 Runtime 的用户任务边界管理。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from Agent.contracts import EventType, ModelProvider, ModelReply, RunEvent, ToolCall
from Agent.errors import AgentExecutionLimitError, AgentInvariantError
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models.errors import is_retryable_model_error
from Agent.retry import ModelRetryPolicy
from Agent.state import MaterializedToolObservation
from context_process import ContextBudgetEstimate, ContextBudgetExceeded
from memory.persistence import SessionPersistenceProjection
from tool import (
    AsyncToolRegistry,
    PreparedToolInvocation,
    ToolContext,
    ToolExecutionObservationError,
    ToolRequestError,
)


@dataclass(frozen=True)
class _PreparedCall:
    position: int
    invocation: PreparedToolInvocation


@dataclass(frozen=True)
class _ToolOutcome:
    position: int
    name: str
    arguments: dict[str, Any]
    tool_call_id: str
    risk: str
    result: str | None = None
    error: BaseException | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    status: str = "success"


class ReactLoop:
    """在一个 Runtime Turn 内执行多次模型调用与工具调用。"""

    def __init__(
        self,
        provider: ModelProvider,
        tools: AsyncToolRegistry,
        hooks: HookRegistry,
        max_steps: int,
        retry_policy: ModelRetryPolicy | None = None,
        max_parallel_tool_calls: int = 4,
    ) -> None:
        self.provider, self.tools, self.hooks, self.max_steps = provider, tools, hooks, max_steps
        self.retry_policy = retry_policy or ModelRetryPolicy(max_attempts=1, delay_seconds=0)
        self.max_parallel_tool_calls = max(1, int(max_parallel_tool_calls))

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
        ephemeral_context_tokens: int | None = None
        task_started_at = time.perf_counter()
        emergency_compression_count = 0
        used_tool_call_ids: set[str] = set()

        successful_steps = 0
        context_loaded = False
        while successful_steps < self.max_steps:
            attempt = 0
            retry_history: list[dict[str, Any]] = []
            while True:
                attempt += 1
                schemas = self.tools.schemas(context)
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
                    render_ephemeral_context = before.data.pop("render_ephemeral_context", None)
                    final_context_budget_check = before.data.pop("final_context_budget_check", None)
                    recover_context_overflow = before.data.pop("recover_context_overflow", None)
                    context_preflight = before.data.pop("context_preflight", None)
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
                    if callable(render_ephemeral_context):
                        render_ephemeral_context(before.data.get("messages"))
                        rendered_messages = before.data.get("messages")
                        if isinstance(rendered_messages, list) and rendered_messages:
                            rendered_content = rendered_messages[-1].get("content")
                            if isinstance(rendered_content, str):
                                ephemeral_context_tokens = max(
                                    0,
                                    _estimate_tokens(rendered_content) - question_tokens,
                                )
                    persist_user = before.data.pop("persist_current_user_operation", None)
                    if callable(persist_user):
                        persisted = persist_user()
                        if inspect.isawaitable(persisted):
                            await persisted
                    messages = before.data.get("messages")
                    schemas = before.data.get("tools")
                    if not isinstance(messages, list) or not isinstance(schemas, list):
                        raise AgentInvariantError("model_before 必须保留列表形式的 messages 和 tools")
                    schemas.sort(key=lambda schema: str(schema.get("name", "")))
                    context_loaded = True
                    final_budget = None
                    if callable(final_context_budget_check):
                        final_budget = final_context_budget_check(messages, schemas)
                        if not isinstance(final_budget, ContextBudgetEstimate):
                            raise AgentInvariantError("final context budget check必须返回ContextBudgetEstimate")
                    estimated_context = (
                        final_budget.estimated_input_tokens
                        if isinstance(final_budget, ContextBudgetEstimate)
                        else _estimate_tokens(json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False))
                    )
                    during = HookEvent(point=HookPoint.MODEL_DURING, session_id=session_id, data={
                        "task": task, "messages": messages, "tools": schemas, "model": model,
                    })
                    await self.hooks.emit(during)
                    logical_model_call_id = during.data.get("logical_model_call_id")
                    model_call_id = during.data.get("model_call_id")
                    operation_attempt_no = int(during.data.get("attempt_no", attempt))
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
                                yield RunEvent(type=EventType.TEXT, payload={
                                    "content": chunk.text,
                                    "logical_model_call_id": logical_model_call_id,
                                    "model_call_id": model_call_id,
                                    "attempt_no": operation_attempt_no,
                                })
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
                        failure_event = HookEvent(
                            point=HookPoint.MODEL_AFTER,
                            session_id=session_id,
                            data=failure,
                        )
                        await self.hooks.emit(failure_event)
                        failure = failure_event.data
                    except Exception as hook_error:
                        _attach_failure_context(hook_error, messages, schemas, model, retry_history)
                        raise
                    if bool(failure.get("context_overflow")):
                        retry_history[-1]["failure_kind"] = "context_overflow"
                        if emergency_compression_count >= 1 or not callable(recover_context_overflow):
                            error = ContextBudgetExceeded(
                                "Provider仍拒绝压缩后的上下文；同一逻辑模型调用最多执行一次应急压缩",
                            )
                            _attach_failure_context(error, messages, schemas, model, retry_history)
                            raise error from exc
                        emergency_compression_count += 1
                        yield RunEvent(
                            type=EventType.COMPRESSION_STARTED,
                            payload={"session_id": session_id, "reason": "provider_context_rejection"},
                        )
                        try:
                            compression_result = await recover_context_overflow(messages, schemas)
                        except Exception as recovery_error:
                            error = recovery_error if isinstance(recovery_error, ContextBudgetExceeded) else ContextBudgetExceeded(
                                f"Provider上下文拒绝后的应急压缩失败：{str(recovery_error) or type(recovery_error).__name__}",
                            )
                            _attach_failure_context(error, messages, schemas, model, retry_history)
                            raise error from recovery_error
                        compression = compression_result.payload()
                        yield RunEvent(
                            type=(
                                EventType.CONTEXT_COMPRESSED
                                if compression.get("status") == "compressed"
                                else EventType.COMPRESSION_FALLBACK
                            ),
                            payload={**compression, "emergency_compression_count": emergency_compression_count},
                        )
                        continue
                    if is_retryable_model_error(exc) and attempt < self.retry_policy.max_attempts:
                        retry_delay = self.retry_policy.delay_seconds * (2 ** (attempt - 1))
                        yield RunEvent(type=EventType.MODEL_RETRY, payload={
                            "attempt": attempt + 1,
                            "max_attempts": self.retry_policy.max_attempts,
                            "delay_seconds": retry_delay,
                            "logical_model_call_id": logical_model_call_id,
                            "model_call_id": model_call_id,
                            "message": str(exc) or type(exc).__name__,
                        })
                        await asyncio.sleep(retry_delay)
                        continue
                    _attach_failure_context(exc, messages, schemas, model, retry_history)
                    raise
                network_failures = [
                    item for item in retry_history
                    if item.get("failure_kind") != "context_overflow"
                ]
                if network_failures:
                    yield RunEvent(type=EventType.MODEL_RECONNECTED, payload={
                        "attempt": attempt,
                        "recovered_failures": len(network_failures),
                        "message": "模型网络连接已恢复，继续当前任务",
                    })
                break

            run_id = str(during.data.get("run_id") or session_id)
            logical_tool_call_parent = str(
                logical_model_call_id or model_call_id or f"model-step-{successful_steps}",
            )
            reply = _ensure_tool_call_ids(
                reply,
                run_id=run_id,
                logical_model_call_id=logical_tool_call_parent,
                used_ids=used_tool_call_ids,
            )
            call_metric = _model_call_metric(
                round((time.perf_counter() - started_at) * 1000, 2),
                estimated_context,
                question_tokens,
                reply,
                ephemeral_context_tokens=ephemeral_context_tokens,
                context_budget=(
                    final_budget.model_dump(mode="python")
                    if isinstance(final_budget, ContextBudgetEstimate)
                    else context_preflight if isinstance(context_preflight, dict)
                    else None
                ),
                emergency_compression_count=emergency_compression_count,
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
            reply = _ensure_tool_call_ids(
                reply,
                run_id=run_id,
                logical_model_call_id=logical_tool_call_parent,
                used_ids=used_tool_call_ids,
                reserve=True,
            )
            if reply.text and not streamed:
                yield RunEvent(type=EventType.TEXT, payload={
                    "content": reply.text,
                    "logical_model_call_id": logical_model_call_id,
                    "model_call_id": model_call_id,
                    "attempt_no": operation_attempt_no,
                })

            if reply.tool_calls:
                prepared_calls = [(call, str(call.id)) for call in reply.tool_calls]
                messages.append(_assistant_tool_message(reply))
                terminal_tool_answer = ""
                try:
                    async for event in self._execute_tools(
                        prepared_calls, messages, context, task, session_id,
                        run_id=run_id,
                        logical_model_call_id=logical_tool_call_parent,
                    ):
                        yield event
                        if event.type is EventType.FINAL:
                            terminal_tool_answer = str(event.payload.get("answer", ""))
                except Exception as exc:
                    _attach_failure_context(exc, messages, schemas, model, [])
                    raise
                if terminal_tool_answer:
                    return
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
        _attach_failure_context(error, messages, self.tools.schemas(context), model, [])
        raise error

    async def _execute_tools(
        self,
        calls: list[tuple[ToolCall, str]],
        messages: list[dict[str, Any]],
        context: ToolContext,
        task: str,
        session_id: str,
        *,
        run_id: str,
        logical_model_call_id: str,
    ) -> AsyncIterator[RunEvent]:
        async for event in self._execute_tools_streaming(
            calls, messages, context, task, session_id,
            run_id=run_id,
            logical_model_call_id=logical_model_call_id,
        ):
            yield event

    async def _execute_tools_streaming(
        self,
        calls: list[tuple[ToolCall, str]],
        messages: list[dict[str, Any]],
        context: ToolContext,
        task: str,
        session_id: str,
        *,
        run_id: str,
        logical_model_call_id: str,
    ) -> AsyncIterator[RunEvent]:
        """Stream left-to-right and flush PURE reads at each serial barrier."""
        pending: list[_PreparedCall] = []
        group_sequence = 0

        async def publish(
            outcome: _ToolOutcome, *, run_after: bool = True,
        ) -> tuple[RunEvent, str]:
            after = HookEvent(point=HookPoint.TOOL_AFTER, session_id=session_id, data={
                "task": task,
                "name": outcome.name,
                "arguments": outcome.arguments,
                "tool_call_id": outcome.tool_call_id,
                "result": outcome.result,
                "error": outcome.error,
                "cancelled": outcome.status == "cancelled",
                "operation_id": outcome.operation_id,
                "attempt_id": outcome.attempt_id,
            })
            if run_after:
                await self.hooks.emit(after)
            if outcome.status == "skipped":
                content, status = str(outcome.result or "工具未执行"), "skipped"
            elif outcome.error is not None:
                content = f"工具执行失败：{str(outcome.error) or type(outcome.error).__name__}"
                status = "cancelled" if outcome.status == "cancelled" else "error"
            else:
                content, status = str(after.data.get("result", outcome.result or "")), "success"
            SessionPersistenceProjection.assert_no_ephemeral(outcome.arguments)
            content = SessionPersistenceProjection.strip_ephemeral(content)

            request_error = isinstance(outcome.error, ToolRequestError) or bool(
                getattr(outcome.error, "tool_request_error", False)
            )
            if (
                outcome.error is not None
                and not request_error
                and not isinstance(outcome.error, ToolExecutionObservationError)
                and outcome.risk != "read"
            ):
                raise outcome.error

            observation_id = f"tool-observation:{run_id}:{outcome.tool_call_id}"
            coordinator = context.operation_coordinator
            materialized = MaterializedToolObservation(
                observation_id=observation_id,
                run_id=run_id,
                operation_id=outcome.operation_id,
                attempt_id=outcome.attempt_id,
                logical_model_call_id=logical_model_call_id,
                tool_call_id=outcome.tool_call_id,
                position=outcome.position,
                name=outcome.name,
                arguments=outcome.arguments,
                status=status,
                finalized_content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                created_at=datetime.now().astimezone().isoformat(timespec="microseconds"),
            )
            if coordinator is not None:
                materialized = coordinator.materialize_tool_observation(materialized)
                content, status = materialized.finalized_content, materialized.status

            publish_event = HookEvent(
                point=HookPoint.TOOL_AFTER,
                session_id=session_id,
                data={
                    **after.data,
                    "result": content,
                    "error": None,
                    "cancelled": status == "cancelled",
                    "observation_status": status,
                    "observation_id": observation_id,
                },
            )
            await self.hooks.publish_tool_observation(publish_event)
            session_record_id = publish_event.data.get("session_record_id")
            if coordinator is not None:
                if not isinstance(session_record_id, str) or not session_record_id:
                    raise AgentInvariantError(
                        "Durable Tool observation was not persisted to the Session",
                    )
                coordinator.mark_tool_observation_published(
                    observation_id, session_record_id,
                )
            _append_tool_message_once(
                messages,
                tool_call_id=outcome.tool_call_id,
                name=outcome.name,
                content=content,
            )
            return RunEvent(type=EventType.TOOL_COMPLETED, payload={
                "name": outcome.name,
                "content": content,
                "status": status,
                "observation_id": observation_id,
            }), content

        async def execute_one(
            item: _PreparedCall,
            *,
            parallel: bool,
            batch_id: str | None,
            prepared_operation: Any | None = None,
        ) -> _ToolOutcome:
            selected = item.invocation
            await self.hooks.emit(HookEvent(
                point=HookPoint.TOOL_DURING,
                session_id=session_id,
                data={
                    "task": task,
                    "name": selected.name,
                    "arguments": selected.arguments,
                    "tool_call_id": selected.tool_call_id,
                },
            ))
            try:
                executed = await self.tools.execute_prepared(
                    selected,
                    context,
                    prepared_operation=prepared_operation,
                    manage_execution_state=not parallel,
                    tool_batch_id=batch_id,
                    tool_call_position=item.position,
                )
                return _ToolOutcome(
                    position=item.position,
                    name=selected.name,
                    arguments=selected.arguments,
                    tool_call_id=selected.tool_call_id,
                    risk=selected.risk,
                    result=executed.result,
                    operation_id=executed.operation_id,
                    attempt_id=executed.attempt_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _ToolOutcome(
                    position=item.position,
                    name=selected.name,
                    arguments=selected.arguments,
                    tool_call_id=selected.tool_call_id,
                    risk=selected.risk,
                    error=exc,
                    operation_id=getattr(exc, "durable_operation_id", None),
                    attempt_id=getattr(exc, "durable_attempt_id", None),
                    status="error",
                )

        async def flush_group() -> AsyncIterator[tuple[RunEvent, str]]:
            nonlocal pending, group_sequence
            if not pending:
                return
            selected, pending = pending, []
            group_sequence += 1
            batch_id = hashlib.sha256(
                f"{run_id}:{logical_model_call_id}:{group_sequence}".encode("utf-8"),
            ).hexdigest()
            yield RunEvent(type=EventType.TOOL_BATCH_STARTED, payload={
                "tool_batch_id": batch_id,
                "tool_call_count": len(selected),
            }), ""
            coordinator = context.operation_coordinator
            prepared_operations: dict[int, Any] = {}
            if coordinator is not None:
                # Ledger identity is established left-to-right before Tool bodies overlap.
                for item in selected:
                    invocation = item.invocation
                    prepared_operations[item.position] = await coordinator.prepare(
                        tool=invocation.tool,
                        name=invocation.name,
                        arguments=invocation.arguments,
                        risk=invocation.risk,
                        context=context,
                        tool_call_id=invocation.tool_call_id,
                        tool_batch_id=batch_id,
                        tool_call_position=item.position,
                    )
            if coordinator is not None:
                await coordinator.begin_parallel_group()
            started_at = time.perf_counter()
            tasks = [
                asyncio.create_task(
                    execute_one(
                        item,
                        parallel=True,
                        batch_id=batch_id,
                        prepared_operation=prepared_operations.get(item.position),
                    ),
                    name=f"tool-{batch_id[:12]}-{item.position}",
                )
                for item in selected
            ]
            try:
                outcomes = await asyncio.gather(*tasks)
            except BaseException:
                # Ordinary Tool failures are converted to _ToolOutcome inside each Task.
                # An exception escaping here is cancellation or a core execution invariant;
                # cancel and settle every sibling before leaving ACTING.
                for task_handle in tasks:
                    if not task_handle.done():
                        task_handle.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            finally:
                if coordinator is not None:
                    await coordinator.finish_parallel_group()
            for outcome in sorted(outcomes, key=lambda item: item.position):
                yield await publish(outcome)
            yield RunEvent(type=EventType.TOOL_BATCH_COMPLETED, payload={
                    "tool_batch_id": batch_id,
                    "tool_call_count": len(selected),
                    "peak_concurrency": len(selected),
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "success_count": sum(item.error is None for item in outcomes),
                    "failure_count": sum(item.error is not None for item in outcomes),
                }), ""

        for position, (call, call_id) in enumerate(calls):
            before = HookEvent(point=HookPoint.TOOL_BEFORE, session_id=session_id, data={
                "task": task,
                "name": call.name,
                "arguments": dict(call.arguments),
                "tool_call_id": call_id,
            })
            await self.hooks.emit(before)
            name, arguments = before.data.get("name"), before.data.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ValueError("tool_before must preserve string name and object arguments")
            try:
                prepared = self.tools.prepare_invocation(
                    name, arguments, context, tool_call_id=call_id,
                )
            except Exception as exc:
                async for event, _ in flush_group():
                    yield event
                request_error = isinstance(exc, ToolRequestError) or bool(
                    getattr(exc, "tool_request_error", False),
                )
                try:
                    failed_risk = self.tools.risk_of(name, arguments)
                except Exception:
                    # Unknown names and malformed dynamic-risk inputs are model request errors.
                    failed_risk = "read"
                    request_error = True
                if not request_error and failed_risk != "read":
                    # Security/availability failures for write or high-risk Tools remain
                    # fail-closed; converting these into model-visible errors could turn a
                    # hard execution guard into a retry loop.
                    raise
                error = exc if isinstance(exc, ToolRequestError) else ToolRequestError(
                    str(exc) or type(exc).__name__,
                )
                event, _ = await publish(_ToolOutcome(
                    position=position,
                    name=name,
                    arguments=arguments,
                    tool_call_id=call_id,
                    risk=failed_risk,
                    error=error,
                    status="error",
                ))
                yield event
                continue
            item = _PreparedCall(position=position, invocation=prepared)
            yield RunEvent(type=EventType.TOOL_REQUESTED, payload={
                "name": prepared.name,
                "arguments": prepared.arguments,
            })
            if prepared.parallel_safe and self.max_parallel_tool_calls > 1:
                pending.append(item)
                if len(pending) >= self.max_parallel_tool_calls:
                    async for event, _ in flush_group():
                        yield event
                continue

            async for event, _ in flush_group():
                yield event
            outcome = await execute_one(item, parallel=False, batch_id=None)
            event, finalized = await publish(outcome)
            yield event
            if outcome.error is None and self.tools.ends_turn(prepared.name, finalized):
                for skipped_position, (unprepared, unprepared_id) in enumerate(
                    calls[position + 1:], start=position + 1,
                ):
                    skipped = f"工具未执行：前序工具 {prepared.name} 已结束当前 Turn。"
                    skipped_event, _ = await publish(_ToolOutcome(
                        position=skipped_position,
                        name=unprepared.name,
                        arguments=dict(unprepared.arguments),
                        tool_call_id=unprepared_id,
                        risk="read",
                        result=skipped,
                        status="skipped",
                    ), run_after=False)
                    yield skipped_event
                answer = (
                    "Harness 已安全合并新的 Tool 源码。当前 Gateway 的 Tool Catalog 仍是旧快照，"
                    "本次运行已正常结束；请重启 Gateway 后在原 Session 继续。"
                )
                yield RunEvent(type=EventType.GATEWAY_RESTART_REQUIRED, payload={
                    "tool": prepared.name,
                    "message": answer,
                })
                yield RunEvent(type=EventType.FINAL, payload={"answer": answer})
                return

        async for event, _ in flush_group():
            yield event


def _assistant_tool_message(reply: ModelReply) -> dict[str, Any]:
    """构造可再次发送给 OpenAI-compatible 接口的 assistant 工具消息。"""
    serialized = [{
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
    } for call in reply.tool_calls]
    return {"role": "assistant", "content": reply.text or None, "tool_calls": serialized}


def _ensure_tool_call_ids(
    reply: ModelReply,
    *,
    run_id: str,
    logical_model_call_id: str,
    used_ids: set[str] | None = None,
    reserve: bool = False,
) -> ModelReply:
    """在任何 model_after 回调前为工具调用补齐稳定 ID。"""
    if not reply.tool_calls:
        return reply
    used: set[str] = set(used_ids or ())
    calls: list[ToolCall] = []
    for position, call in enumerate(reply.tool_calls):
        selected = str(call.id or "")
        if not selected or selected in used:
            digest = hashlib.sha256(
                f"{run_id}:{logical_model_call_id}:{position}".encode("utf-8"),
            ).hexdigest()[:24]
            selected = f"call_{digest}"
            suffix = 0
            while selected in used:
                suffix += 1
                selected = f"call_{digest}_{suffix}"
        used.add(selected)
        calls.append(ToolCall(
            name=call.name,
            arguments=dict(call.arguments),
            id=selected,
        ))
    calls_tuple = tuple(calls)
    if reserve and used_ids is not None:
        used_ids.update(str(call.id) for call in calls_tuple if call.id)
    if calls_tuple == reply.tool_calls:
        return reply
    return ModelReply(text=reply.text, tool_calls=calls_tuple, finished=reply.finished, usage=reply.usage, reasoning=reply.reasoning)


def _append_tool_message_once(
    messages: list[dict[str, Any]],
    *,
    tool_call_id: str,
    name: str,
    content: str,
) -> None:
    existing = [
        item for item in messages
        if item.get("role") == "tool" and item.get("tool_call_id") == tool_call_id
    ]
    if existing:
        current = existing[-1]
        if current.get("name") != name or current.get("content") != content:
            raise AgentInvariantError(
                f"Tool observation content conflict: {tool_call_id}",
            )
        return
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content,
    })


def _model_call_metric(
    latency_ms: float,
    context_tokens: int,
    question_tokens: int,
    reply: ModelReply,
    *,
    ephemeral_context_tokens: int | None = None,
    context_budget: dict[str, Any] | None = None,
    emergency_compression_count: int = 0,
) -> dict[str, Any]:
    """生成一次无编号模型 API 调用的审计指标。"""
    serialized_calls = json.dumps([{"name": call.name, "arguments": call.arguments} for call in reply.tool_calls], ensure_ascii=False)
    usage = reply.usage
    total_input = usage.input_tokens if usage and usage.input_tokens is not None else context_tokens
    cached_input = usage.cached_input_tokens if usage and usage.cached_input_tokens is not None else None
    metric = {
        "latency_ms": latency_ms,
        "input_tokens": {
            "context_total": total_input,
            "cached": cached_input,
            "cache_hit_ratio": (
                cached_input / total_input
                if cached_input is not None and total_input > 0
                else None
            ),
            "current_question": question_tokens,
            "ephemeral_context": ephemeral_context_tokens,
            "context_source": "provider" if usage and usage.input_tokens is not None else "estimated",
            "current_question_source": "estimated",
        },
        "output_tokens": usage.output_tokens if usage and usage.output_tokens is not None else _estimate_tokens(reply.text + serialized_calls),
        "output_tokens_source": "provider" if usage and usage.output_tokens is not None else "estimated",
        "emergency_compression_count": emergency_compression_count,
    }
    if context_budget is not None:
        metric["context_budget"] = context_budget
    return metric


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
            "messages": SessionPersistenceProjection.from_runtime_messages(messages),
            "tools": copy.deepcopy(schemas),
            "model": copy.deepcopy(model),
            "retry_history": copy.deepcopy(retry_history),
        })
    except Exception:
        return
