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
from memory.provider_context import ProviderContextRecord, context_baseline, effective_contexts
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
    # Mutable only at first call / compaction boundary; never re-render a
    # committed Turn on a network retry or subsequent tool/model iteration.
    provider_turns: dict[str, dict] = {}
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
            provider_turns[event.session_id] = {}
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
        turn = provider_turns.setdefault(event.session_id, {})
        # A getter observes persistence later in this same MODEL_BEFORE pass,
        # including a Provider rejection on the first model call.
        event.data["current_user_record_id"] = lambda: turn.get("record_id")
        if fragments is not None:
            event.data["register_provider_context_fragment"] = lambda name, content: fragments.set(
                event.session_id, name, content,
            )
            event.data["set_continuity_fragment"] = lambda summary: fragments.set(
                event.session_id,
                "continuity",
                '<continuity_fragment ephemeral="true">\n' + str(summary).strip()
                + '\n</continuity_fragment>',
                once=False,
            )
            # Conversation continuity is mandatory, independent of optional recall.
            summary = memory.latest_summary(event.session_id)
            if summary and not turn.get("continuity_target_record_id"):
                event.data["set_continuity_fragment"](summary)
            else:
                fragments.remove(event.session_id, "continuity")

            def place_compressed_continuity(
                selected_summary: str,
                target_record_id: str | None,
            ) -> None:
                continuity = (
                    '<continuity_fragment ephemeral="true">\n'
                    + str(selected_summary).strip()
                    + '\n</continuity_fragment>'
                )
                if target_record_id is None:
                    turn.pop("continuity_target_record_id", None)
                    fragments.set(event.session_id, "continuity", continuity, once=False)
                    return
                records = memory.session_context_records(event.session_id)
                users = {
                    str(record.get("record_id")): record
                    for record in records
                    if record.get("role") == "user" and isinstance(record.get("record_id"), str)
                }
                target = users.get(target_record_id)
                if target is None:
                    raise ValueError("Compressed continuity target is not visible in the new Session segment")
                existing = effective_contexts(records).get(target_record_id)
                target_fragments = dict(existing.fragments) if existing is not None else {}
                target_fragments["continuity"] = continuity
                packet = ProviderContextRecord.create(
                    str(target.get("content", "")),
                    memory.active_path(event.session_id).name,
                    target_fragments,
                    origin_refs=origin_refs,
                )
                identity = hashlib.sha256(
                    (
                        f"{event.session_id}:{memory.active_path(event.session_id).name}:"
                        f"{target_record_id}:{packet.content_hash}"
                    ).encode()
                ).hexdigest()
                memory.sessions.append_once(event.session_id, {
                    "role": "provider_context",
                    "content": None,
                    "record_id": f"provider-context:{identity}",
                    "timestamp": memory.session_created_at(event.session_id),
                    "context_target_record_id": target_record_id,
                    "provider_context": packet.model_dump(mode="json"),
                    **origin_refs,
                })
                turn["continuity_target_record_id"] = target_record_id
                fragments.remove(event.session_id, "continuity")

            event.data["place_compressed_continuity"] = place_compressed_continuity

        def rebuild_messages(*, refresh_system: bool = False) -> list[dict[str, object]]:
            nonlocal base_system
            if refresh_system and prompts is not None:
                base_system = {"role": "system", "content": prompts.refresh(event.session_id).content}
                base_systems[event.session_id] = dict(base_system)
            system = dict(base_system)
            rebuilt: list[dict[str, object]] = [
                system,
                *memory.restore_messages(event.session_id, provider_context=True),
            ]
            if first_model_call:
                rebuilt.append(current_user_message or {"role": "user", "content": task})
            return rebuilt

        if first_model_call:
            messages[:] = rebuild_messages()
            def persist_current_user() -> str:
                if turn.get("record_id"):
                    return turn["record_id"]
                packet = turn.get("packet")
                turn["record_id"] = memory.record_user(
                    event.session_id,
                    task,
                    origin=session_origin,
                    audit=_audit(event),
                    provider_context=packet.model_dump(mode="json") if packet else None,
                )
                return turn["record_id"]
            event.data["persist_current_user_operation"] = persist_current_user
        if callable(render_provider_query):
            def prepare_packet() -> ProviderContextRecord:
                records = memory.session_context_records(event.session_id)
                # When compaction retained the current query, its replacement
                # projection belongs to the new epoch. Compare only against
                # preceding visible messages, not the old projection itself.
                if turn.get("record_id"):
                    if not any(r.get("record_id") == turn["record_id"] for r in records):
                        raise ValueError("Compaction lost the current user record")
                return dynamic.prepare(task, event.session_id, origin_refs=origin_refs,
                                       context_epoch=memory.active_path(event.session_id).name,
                                       baseline=context_baseline(
                                           records,
                                           before_record_id=turn.get("record_id"),
                                       ))

            def render_ephemeral_context(target_messages: list[dict[str, object]]) -> None:
                epoch = memory.active_path(event.session_id).name
                if turn.get("epoch") == epoch:
                    return
                selected = next((
                    message for message in reversed(target_messages)
                    if message.get("role") == "user"
                    and message.get("content") in {task, turn.get("rendered")}
                ), None)
                if selected is None and turn.get("record_id"):
                    records = memory.session_context_records(event.session_id)
                    old = effective_contexts(records).get(turn["record_id"])
                    old_text = old.render(task) if old else task
                    selected = next((m for m in reversed(target_messages)
                                     if m.get("role") == "user" and m.get("content") == old_text), None)
                if selected is None:
                    raise ValueError("Current user query missing during context reconstruction")
                packet = prepare_packet()
                rendered = packet.render(task)
                if turn.get("record_id"):
                    # A mid-Turn/emergency compaction is an explicit history
                    # boundary. Append an amendment; never overwrite the user.
                    identity = hashlib.sha256(
                        f'{event.session_id}:{epoch}:{turn["record_id"]}'.encode()
                    ).hexdigest()
                    memory.sessions.append_once(event.session_id, {
                        "role": "provider_context", "content": None,
                        "record_id": f"provider-context:{identity}",
                        "timestamp": memory.session_created_at(event.session_id),
                        "context_target_record_id": turn["record_id"],
                        "provider_context": packet.model_dump(mode="json"),
                        **origin_refs,
                    })
                selected["content"] = rendered
                turn.update(packet=packet, rendered=rendered, epoch=epoch)
                dynamic.last_envelope_hash = packet.content_hash
                dynamic.injection_count += bool(packet.fragments)

            event.data["render_ephemeral_context"] = render_ephemeral_context
            if callable(preview_provider_query):
                event.data["preview_ephemeral_context"] = lambda: (
                    turn["rendered"] if turn.get("epoch") == memory.active_path(event.session_id).name
                    else prepare_packet().render(task)
                )
        # Summary/Profile are provider-tail facts; compression must not rebuild the stable prefix.
        event.data["reload_messages_after_compression"] = lambda: rebuild_messages(refresh_system=False)

        def rebuild_after_emergency() -> list[dict[str, object]]:
            # The retry starts a fresh MODEL_BEFORE pass, which renders the
            # current provider query after all restored amendments are visible.
            return [dict(base_system), *memory.restore_messages(event.session_id, provider_context=True)]

        event.data["reload_messages_after_emergency_compression"] = rebuild_after_emergency

    async def clear_context_state(event: HookEvent) -> None:
        base_systems.pop(event.session_id, None)
        turn_snapshots.pop(event.session_id, None)
        access_snapshots.pop(event.session_id, None)
        provider_turns.pop(event.session_id, None)
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
            recall_summaries=bool(getattr(config, "memory_recall_summaries", False)),
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

    registry.register(HookPoint.TRACE_START, create_or_restore_session, priority=-100)
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
    audit = {
        key: selected
        for key in ("run_id", "turn_id", "operation_id")
        if isinstance((selected := value.get(key)), str) and selected
    }
    ui_context = event.data.get("ui_context")
    if isinstance(ui_context, dict):
        audit["ui_context"] = ui_context
    return audit
