"""把自动压缩与失败裁剪注册到现有 Hook 生命周期。"""

from __future__ import annotations

from Agent.contracts import ModelReply
from Agent.hook import HookEvent, HookPoint, HookRegistry
from .budget import is_context_length_error
from .compression import ContextProcessor


def register_context_callbacks(registry: HookRegistry, processor: ContextProcessor) -> None:
    """在每次真实模型请求前检查完整上下文并安排压缩。"""

    async def schedule_compression_before_model(event: HookEvent) -> None:
        messages = event.data.get("messages")
        tools = event.data.get("tools")
        threshold = processor.config.compression_threshold_tokens
        if not isinstance(messages, list) or not isinstance(tools, list):
            return
        preview = None
        preview_callback = event.data.get("preview_ephemeral_context")
        if callable(preview_callback):
            preview = preview_callback()
            if not isinstance(preview, str):
                raise ValueError("ephemeral context preview必须返回字符串")
        estimate = processor.forecast(
            event.session_id,
            messages,
            tools,
            ephemeral_preview=preview,
        )
        event.data["context_preflight"] = estimate.model_dump(mode="python")

        event.data["final_context_budget_check"] = lambda selected_messages, selected_tools: (
            processor.finalize_request(event.session_id, selected_messages, selected_tools)
        )
        emergency_reload = event.data.get("reload_messages_after_emergency_compression")
        event.data["recover_context_overflow"] = lambda selected_messages, selected_tools: (
            processor.recover_from_overflow(
                event.session_id,
                selected_messages,
                selected_tools,
                current_query=str(event.data.get("task", "")),
                reload_messages=emergency_reload if callable(emergency_reload) else None,
            )
        )
        if threshold <= 0:
            return
        if processor.fallback_active(event.session_id):
            if processor.trim_messages_if_needed(event.session_id, messages):
                event.data["context_trimmed"] = True
            return
        if estimate.decision == "proceed" or not processor.memory.has_compressible_history(event.session_id):
            return
        reload_messages = event.data.get("reload_messages_after_compression")
        event.data["compression_operation"] = lambda: processor.prepare_before_model(
            event.session_id,
            messages,
            tools,
            reload_messages=reload_messages if callable(reload_messages) else None,
            ephemeral_preview=preview,
            current_query=str(event.data.get("task", "")),
            reason=estimate.reason,
        )

    async def calibrate_after_model(event: HookEvent) -> None:
        reply = event.data.get("reply")
        if isinstance(reply, ModelReply):
            usage = reply.usage
            estimate = processor.budget.last_estimate(event.session_id)
            if estimate is not None:
                calibration = processor.update_from_provider_usage(
                    event.session_id,
                    estimated_input_tokens=estimate.estimated_input_tokens,
                    provider_input_tokens=usage.input_tokens if usage is not None else None,
                )
                event.data["context_usage_calibration"] = calibration.model_dump(mode="python")
                model_call = event.data.get("model_call")
                if isinstance(model_call, dict):
                    model_call["estimation_error_ratio"] = calibration.last_error_ratio
            return
        error = event.data.get("error")
        if isinstance(error, BaseException) and is_context_length_error(error):
            event.data["context_overflow"] = True

    async def record_turn_pressure(event: HookEvent) -> None:
        estimate = processor.budget.last_estimate(event.session_id)
        if estimate is not None:
            event.data["context_pressure"] = {
                "pressure_ratio": estimate.pressure_ratio,
                "message_count": estimate.message_count,
                "decision": estimate.decision,
                "reason": estimate.reason,
            }

    registry.register(HookPoint.MODEL_BEFORE, schedule_compression_before_model, priority=0)
    registry.register(HookPoint.MODEL_AFTER, calibrate_after_model, priority=-50)
    registry.register(HookPoint.TURN_END, record_turn_pressure, priority=50)
