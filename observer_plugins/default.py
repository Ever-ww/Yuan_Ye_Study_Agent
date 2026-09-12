"""Conservative recursive Observer reducer over user-visible events only."""

from __future__ import annotations

from Agent.observer import (
    IntentAlignment,
    IntentAlignmentStatus,
    ObserverState,
    VisibleObserverEvent,
)


class RecursiveVisibleObserver:
    plugin_id = "builtin.observer"
    plugin_version = "1"
    state_schema_version = 1

    def initial_state(self, user_problem: str) -> ObserverState:
        return ObserverState(
            user_problem=user_problem.strip(),
            in_progress_task="理解用户问题",
            current_agent_action="准备执行",
        )

    def reduce(
        self, previous: ObserverState, event: VisibleObserverEvent,
    ) -> ObserverState:
        completed = list(previous.completed_tasks)
        in_progress = previous.in_progress_task
        action = previous.current_agent_action
        alignment = previous.intent_alignment
        if event.event_type in {"run_queued", "run_started"}:
            in_progress = "处理当前请求"
            action = "任务已开始"
        elif event.event_type == "text" and event.content:
            action = _short(event.content)
            in_progress = "生成并核对回答"
        elif event.event_type == "tool_requested" and event.tool_name:
            action = f"调用工具 {event.tool_name}"
            in_progress = action
        elif event.event_type == "tool_completed" and event.tool_name:
            item = f"工具 {event.tool_name}：{event.tool_status or 'completed'}"
            completed.append(item)
            completed = completed[-20:]
            action = item
            in_progress = "继续处理当前请求"
        elif event.event_type in {"run_failed", "run_cancelled", "run_interrupted"}:
            action = _short(event.content) or "运行未正常完成"
            in_progress = ""
            alignment = IntentAlignment(
                status=IntentAlignmentStatus.UNCERTAIN,
                reason="运行未正常完成，无法确认最终意图对齐",
            )
        elif event.event_type in {"final", "run_completed"}:
            if event.content:
                completed.append(_short(event.content))
                completed = completed[-20:]
            action = "已完成当前回答"
            in_progress = ""
        return ObserverState(
            user_problem=previous.user_problem,
            completed_tasks=tuple(completed),
            in_progress_task=in_progress,
            current_agent_action=action,
            intent_alignment=alignment,
        )

    def migrate_state(
        self, state: dict[str, object], from_schema_version: int,
    ) -> ObserverState:
        if from_schema_version != self.state_schema_version:
            raise ValueError(
                f"unsupported Observer state migration: {from_schema_version}"
            )
        return ObserverState.model_validate(state, strict=True)


def create_observer_plugin() -> RecursiveVisibleObserver:
    return RecursiveVisibleObserver()


def _short(value: str, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"

