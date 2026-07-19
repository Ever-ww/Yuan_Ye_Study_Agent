"""把记忆能力注册为普通 Hook 回调函数，不定义专用 Hook 类型。"""

from __future__ import annotations

from Agent.hook import HookEvent, HookPoint, HookRegistry
from memory.store import MemoryStore


def register_memory_callbacks(registry: HookRegistry, memory: MemoryStore) -> None:
    """注册会话创建、上下文加载和最终回复持久化回调。"""

    async def create_or_restore_session(event: HookEvent) -> None:
        if memory.has_session(event.session_id):
            return
        if not event.data.get("new_session"):
            raise KeyError(f"未知会话：{event.session_id}")
        memory.create_session(str(event.data.get("task", "")), session_id=event.session_id)

    async def load_context(event: HookEvent) -> None:
        if not event.data.get("first_model_call"):
            return
        messages = event.data.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            raise ValueError("Memory 回调需要基础 system/user 消息")
        system = dict(messages[0])
        profile = memory.profile_context()
        if profile:
            system["content"] = f"{system.get('content', '')}\n\n用户长期记忆：\n{profile[:6000]}"
        task = str(event.data.get("task", ""))
        messages[:] = [system, *memory.restore_messages(event.session_id), {"role": "user", "content": task}]
        memory.record_user(event.session_id, task)

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

    registry.register(HookPoint.TRACE_START, create_or_restore_session, priority=-100)
    registry.register(HookPoint.MODEL_BEFORE, load_context, priority=-100)
    registry.register(HookPoint.TURN_END, persist_answer, priority=100)
