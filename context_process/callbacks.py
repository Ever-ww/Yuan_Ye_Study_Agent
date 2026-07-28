"""把自动压缩与失败裁剪注册到现有 Hook 生命周期。"""

from __future__ import annotations

from Agent.hook import HookEvent, HookPoint, HookRegistry
from .compression import ContextProcessor


def register_context_callbacks(registry: HookRegistry, processor: ContextProcessor) -> None:
    """在每次真实模型请求前检查完整上下文并安排压缩。"""

    async def schedule_compression_before_model(event: HookEvent) -> None:
        messages = event.data.get("messages")
        tools = event.data.get("tools")
        threshold = processor.config.compression_threshold_tokens
        if threshold <= 0 or not isinstance(messages, list) or not isinstance(tools, list):
            return
        if processor.fallback_active(event.session_id):
            if processor.trim_messages_if_needed(event.session_id, messages):
                event.data["context_trimmed"] = True
            return
        if not processor.should_compress(event.session_id, messages, tools):
            return
        reload_messages = event.data.get("reload_messages_after_compression")
        event.data["compression_operation"] = lambda: processor.prepare_before_model(
            event.session_id,
            messages,
            tools,
            reload_messages=reload_messages if callable(reload_messages) else None,
        )

    registry.register(HookPoint.MODEL_BEFORE, schedule_compression_before_model, priority=0)
