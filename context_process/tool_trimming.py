"""Independent MODEL_BEFORE projection for oversized historical Tool output."""

from __future__ import annotations

from typing import Any, Callable

from Agent.hook import HookEvent, HookPoint, HookRegistry
from memory.store import MemoryStore
from memory.tool_projection import ToolOutputProjectionPolicy, ToolOutputProjector


ToolOutputTrimmer = Callable[[list[dict[str, Any]]], int]


def register_tool_output_trimming_callbacks(
    registry: HookRegistry,
    memory: MemoryStore,
    config: Any,
    *,
    session_read_available: bool,
) -> None:
    """Trim only Provider message copies and expose a post-reload callback.

    Compression may replace the in-memory message list after this Hook has run.
    The callback is therefore left on the Hook event so the compression Hook can
    apply the exact same projection to the newly restored list.
    """

    async def trim_historical_tool_output(event: HookEvent) -> None:
        messages = event.data.get("messages")
        if not isinstance(messages, list) or not session_read_available:
            return
        records = memory.session_context_records_with_locations(event.session_id)
        projector = ToolOutputProjector(
            ToolOutputProjectionPolicy.from_config(config),
            records,
            current_run_id=_run_id(event),
        )

        def trim(selected_messages: list[dict[str, Any]]) -> int:
            if not isinstance(selected_messages, list):
                raise TypeError("Tool output trimming requires a message list")
            return projector.project(selected_messages, protect_current_turn=True)

        event.data["trim_historical_tool_outputs"] = trim
        saved_chars = trim(messages)
        event.data["tool_output_trimming"] = {
            "saved_chars": saved_chars,
            "cjk_threshold_chars": projector.policy.cjk_threshold_chars,
            "english_threshold_words": projector.policy.english_threshold_words,
            "head_chars": projector.policy.head_chars,
            "tail_chars": projector.policy.tail_chars,
        }

    # Memory reconstructs the canonical message projection at -100.  Trimming
    # follows it and precedes context forecasting/compression at priority 0.
    registry.register(
        HookPoint.MODEL_BEFORE,
        trim_historical_tool_output,
        priority=-20,
        identity="historical_tool_output_trimming",
    )


def _run_id(event: HookEvent) -> str | None:
    durable = event.data.get("durable_audit")
    if not isinstance(durable, dict):
        return None
    value = durable.get("run_id")
    return str(value) if isinstance(value, str) and value else None
