"""把自动压缩与失败裁剪注册到现有 Hook 生命周期。"""

from __future__ import annotations

from Agent.hook import HookEvent, HookPoint, HookRegistry
from .compression import ContextProcessor


def register_context_callbacks(registry: HookRegistry, processor: ContextProcessor) -> None:
    """在 model_before 裁剪，在最终 turn_end 检查并执行自动压缩。"""

    async def trim_failed_context(event: HookEvent) -> None:
        messages = event.data.get("messages")
        if isinstance(messages, list) and processor.trim_messages_if_needed(event.session_id, messages):
            event.data["context_trimmed"] = True

    async def schedule_compression_after_answer(event: HookEvent) -> None:
        threshold = processor.config.compression_threshold_tokens
        if threshold <= 0 or event.data.get("error") is not None or not event.data.get("completed"):
            return
        calls = event.data.get("model_calls", [])
        totals = []
        for call in calls if isinstance(calls, list) else []:
            inputs = call.get("input_tokens", {}) if isinstance(call, dict) else {}
            context_total = inputs.get("context_total", 0) if isinstance(inputs, dict) else 0
            output = call.get("output_tokens", 0) if isinstance(call, dict) else 0
            if isinstance(context_total, (int, float)) and isinstance(output, (int, float)):
                totals.append(context_total + output)
        if not totals or max(totals) < threshold:
            return
        event.data["compression_operation"] = lambda: processor.compress(event.session_id)

    registry.register(HookPoint.MODEL_BEFORE, trim_failed_context, priority=0)
    registry.register(HookPoint.TURN_END, schedule_compression_after_answer, priority=200)
