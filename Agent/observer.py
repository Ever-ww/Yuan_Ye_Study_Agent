"""Contracts for the hot-pluggable Runtime Observer capability.

The Observer reducer is a capability-layer plugin.  Durable state, event
visibility, offsets, approval and recovery are Gateway Core responsibilities.
No object in this module exposes tools, prompts, hidden context or reasoning.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class IntentAlignmentStatus(str, Enum):
    ALIGNED = "aligned"
    UNCERTAIN = "uncertain"
    DRIFTED = "drifted"


class IntentAlignment(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: IntentAlignmentStatus = IntentAlignmentStatus.ALIGNED
    reason: str = ""


class ObserverState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    user_problem: str = ""
    completed_tasks: tuple[str, ...] = ()
    in_progress_task: str = ""
    current_agent_action: str = ""
    intent_alignment: IntentAlignment = Field(default_factory=IntentAlignment)


class VisibleObserverEvent(BaseModel):
    """A deliberately lossy, user-visible projection of one Gateway Event."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    event_id: str
    run_id: str
    stream_sequence: int = Field(ge=1)
    event_type: str
    occurred_at: str
    content: str = ""
    tool_name: str | None = None
    tool_status: str | None = None


class ObserverToolLoop(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    loop: int = Field(ge=1)
    execution: str = Field(pattern=r"^(serial|parallel|mixed)$")
    tools: tuple[str, ...] = ()


class ObserverEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    evidence_id: str
    run_id: str
    session_id: str | None = None
    generation_id: str
    observer_plugin_id: str
    observer_plugin_version: str
    state_schema_version: int = Field(ge=1)
    runtime_role: str
    agent_role: str
    runtime_profile: str
    trigger: str
    user_problem: str
    completed_tasks: tuple[str, ...] = ()
    visible_execution_summary: str = ""
    user_correction: str | None = None
    final_intent_alignment: IntentAlignment
    adopted_correction_prompt: str | None = None
    tool_loops: tuple[ObserverToolLoop, ...] = ()
    finalized_at: str


class ObserverPlugin(Protocol):
    plugin_id: str
    plugin_version: str
    state_schema_version: int

    def initial_state(self, user_problem: str) -> ObserverState: ...

    def reduce(
        self, previous: ObserverState, event: VisibleObserverEvent,
    ) -> ObserverState: ...

    def migrate_state(
        self, state: dict[str, object], from_schema_version: int,
    ) -> ObserverState: ...


def render_observer_progress(state: ObserverState) -> str:
    completed = "\n".join(f"- {item}" for item in state.completed_tasks) or "- 暂无"
    progress = f"- {state.in_progress_task}" if state.in_progress_task else "- 暂无"
    problem = state.user_problem.strip() or "尚未识别"
    return (
        "## 用户问题\n\n"
        f"- {problem}\n\n"
        "## 已完成\n\n"
        f"{completed}\n\n"
        "## 进行中\n\n"
        f"{progress}"
    )


__all__ = [
    "IntentAlignment", "IntentAlignmentStatus", "ObserverEvidence",
    "ObserverPlugin", "ObserverState", "ObserverToolLoop",
    "VisibleObserverEvent", "render_observer_progress",
]
