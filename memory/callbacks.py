"""把记忆能力注册为普通 Hook 回调函数，不定义专用 Hook 类型。"""

from __future__ import annotations

import json
import hashlib
from typing import Literal

from Agent.contracts import ModelReply
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models.errors import is_retryable_model_error
from memory.store import MemoryStore
from memory.long_term import MemoryTurnSnapshot
from memory.retrieval import (
    MemoryContextProjector,
    MemoryQueryBuilder,
    access_snapshot,
    project_identity,
)
from prompt import PromptComposer


def register_memory_callbacks(
    registry: HookRegistry,
    memory: MemoryStore,
    prompts: PromptComposer | None = None,
    *,
    session_origin: Literal["interactive", "cron", "maintenance"] = "interactive",
    runtime_profile: Literal["interactive", "cron", "harness", "maintenance", "memoryless"] | None = None,
) -> None:
    """注册会话创建、上下文加载和最终回复持久化回调。"""
    base_systems: dict[str, dict[str, object]] = {}
    turn_snapshots: dict[str, MemoryTurnSnapshot] = {}
    access_snapshots: dict[str, object] = {}
    selected_profile = runtime_profile or (
        "cron" if session_origin == "cron" else
        "maintenance" if session_origin == "maintenance" else "interactive"
    )

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
        task = str(event.data.get("task", ""))
        render_provider_query = getattr(prompts, "render_provider_query", None)
        preview_provider_query = getattr(prompts, "preview_provider_query", None)
        origin_refs = _audit(event)
        dynamic = getattr(prompts, "dynamic_context", None)
        fragments = getattr(dynamic, "fragments", None)
        if fragments is not None:
            event.data["set_continuity_fragment"] = lambda summary: fragments.set(
                event.session_id,
                "continuity",
                '<continuity_fragment ephemeral="true">\n' + str(summary).strip()
                + '\n</continuity_fragment>',
                once=True,
            )

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
        if callable(render_provider_query):
            def render_ephemeral_context(target_messages: list[dict[str, object]]) -> None:
                selected = next((
                    message for message in reversed(target_messages)
                    if message.get("role") == "user" and message.get("content") == task
                ), None)
                if selected is None:
                    # The current Turn was already rendered and no compression
                    # reload replaced it. Do not duplicate the envelope.
                    return
                original = selected.get("content")
                if not isinstance(original, str):
                    raise ValueError("Agent user query must be text")
                selected["content"] = render_provider_query(
                    original,
                    event.session_id,
                    origin_refs=origin_refs,
                )

            event.data["render_ephemeral_context"] = render_ephemeral_context
            if callable(preview_provider_query):
                event.data["preview_ephemeral_context"] = lambda: preview_provider_query(
                    task,
                    event.session_id,
                    origin_refs=origin_refs,
                )
        # Summary/Profile are provider-tail facts; compression must not rebuild the stable prefix.
        event.data["reload_messages_after_compression"] = lambda: rebuild_messages(refresh_system=False)

        def rebuild_after_emergency() -> list[dict[str, object]]:
            rebuilt = [dict(base_system), *memory.restore_messages(event.session_id)]
            if callable(render_provider_query):
                for message in reversed(rebuilt):
                    if message.get("role") == "user" and message.get("content") == task:
                        message["content"] = render_provider_query(
                            task,
                            event.session_id,
                            origin_refs=origin_refs,
                        )
                        break
            return rebuilt

        event.data["reload_messages_after_emergency_compression"] = rebuild_after_emergency

    async def clear_context_state(event: HookEvent) -> None:
        base_systems.pop(event.session_id, None)
        turn_snapshots.pop(event.session_id, None)
        access_snapshots.pop(event.session_id, None)
        dynamic = getattr(prompts, "dynamic_context", None)
        fragments = getattr(dynamic, "fragments", None)
        if fragments is not None:
            fragments.clear(event.session_id)

    async def retrieve_turn_memory(event: HookEvent) -> None:
        """Freeze visibility at TURN_START; failures degrade to empty recall."""
        dynamic = getattr(prompts, "dynamic_context", None)
        fragments = getattr(dynamic, "fragments", None)
        if fragments is None or not hasattr(memory, "memory_retriever"):
            return
        audit = _audit(event)
        config = event.data.get("config")
        if config is not None and not bool(getattr(config, "memory_retrieval_enabled", True)):
            fragments.remove(event.session_id, "memory")
            return
        workspace = str(getattr(config, "workspace_root", memory.workspace_root))
        if selected_profile == "harness" and hasattr(memory, "memory_identity_root"):
            workspace = str(memory.memory_identity_root)
        run_id = audit.get("run_id")
        turn_id = audit.get("turn_id")
        cron_access = str(
            event.data.get("memory_access", getattr(memory, "runtime_memory_access", "none"))
        )
        allowed_kinds = getattr(memory, "runtime_allowed_memory_kinds", ())
        access = access_snapshot(
            runtime_profile=selected_profile,
            workspace_root=workspace,
            session_id=event.session_id,
            run_id=run_id,
            cron_memory_access=cron_access,
            allowed_kinds=allowed_kinds,
        )
        access_snapshots[event.session_id] = access
        try:
            # Help the eventually-consistent local index catch up before the
            # visibility watermark is frozen. This is idempotent and never
            # crosses the canonical/index database transaction boundary.
            memory.memory_index_worker.reconcile(limit=100)
            recent = []
            for message in memory.restore_messages(event.session_id)[-6:]:
                if message.get("role") not in {"user", "assistant", "summary"}:
                    continue
                content = message.get("content")
                if isinstance(content, str) and content:
                    recent.append(content)
            query = MemoryQueryBuilder.build(
                str(event.data.get("task", "")),
                project_identity=project_identity(workspace),
                objective=str(event.data.get("active_objective", "")),
                recent_context="\n".join(recent),
                origin_refs=tuple(str(value) for value in audit.values()),
            )
            snapshot = await memory.memory_retriever.retrieve_async(
                query,
                access,
                session_id=event.session_id,
                run_id=run_id,
                turn_id=turn_id,
            )
        except Exception as exc:
            # Recall is an enhancement. Store corruption, scope ambiguity, or
            # retrieval implementation errors must not kill a normal Agent Turn.
            snapshot = MemoryTurnSnapshot(
                snapshot_id="mrs_degraded_" + hashlib.sha256(
                    f"{event.session_id}:{type(exc).__name__}".encode()
                ).hexdigest()[:16],
                session_id=event.session_id,
                run_id=run_id,
                turn_id=turn_id,
                store_watermark=0,
                query_hash=hashlib.sha256(str(event.data.get("task", "")).encode()).hexdigest(),
                token_budget=access.profile.token_budget,
                used_tokens=0,
                degradation_reason=f"retrieval_failed:{type(exc).__name__}",
                created_at=access.created_at,
            )
        turn_snapshots[event.session_id] = snapshot
        fragments.set(event.session_id, "memory", MemoryContextProjector.render(snapshot))
        event.data["memory_turn_snapshot"] = snapshot.model_dump(mode="python")

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

    async def record_retrieval_feedback(event: HookEvent) -> None:
        snapshot = turn_snapshots.get(event.session_id)
        structured = getattr(memory, "structured", None)
        if snapshot is None or structured is None:
            return
        try:
            structured.record_retrieval_feedback(
                snapshot.snapshot_id,
                outcome=(
                    "cancelled" if event.data.get("cancelled") else
                    "failed" if event.data.get("error") is not None else
                    "completed" if event.data.get("completed") else "incomplete"
                ),
                selected_memory_ids=[item.record.memory_id for item in snapshot.selected],
            )
        except Exception:
            memory.memory_retriever.retrieval_audit_failures += 1

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
        observation_status = event.data.get("observation_status")
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
            status=(
                str(observation_status)
                if observation_status in {"success", "error", "cancelled", "skipped"}
                else "cancelled" if cancelled else "success" if error is None else "error"
            ),
            arguments=dict(event.data.get("arguments", {})),
            record_id=(
                str(event.data["observation_id"])
                if event.data.get("observation_id") else None
            ),
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
    registry.register(HookPoint.TURN_START, retrieve_turn_memory, priority=-70)
    registry.register(HookPoint.MODEL_BEFORE, load_context, priority=-100)
    registry.register(HookPoint.MODEL_AFTER, persist_model_tool_calls, priority=100)
    registry.register_tool_observation_publisher(persist_tool_result, priority=100)
    registry.register(HookPoint.TURN_END, persist_answer, priority=100)
    registry.register(HookPoint.TURN_END, record_retrieval_feedback, priority=110)
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
