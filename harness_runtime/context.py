"""Trace scoping, ephemeral query rendering, and persistence guards."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from Agent.hook import HookEvent, HookPoint, HookRegistry
from memory.persistence import SessionPersistenceProjection

from .models import (
    EPHEMERAL_CONTEXT_CLOSE,
    EPHEMERAL_CONTEXT_OPEN,
    EphemeralHarnessContextEnvelope,
    HarnessTraceContext,
)


_CURRENT_TRACE: ContextVar[HarnessTraceContext | None] = ContextVar(
    "yy_harness_trace",
    default=None,
)


def current_harness_trace() -> HarnessTraceContext | None:
    return _CURRENT_TRACE.get()


class HarnessTraceContextScope:
    def __init__(self, trace: HarnessTraceContext) -> None:
        self.trace = trace
        self._token: Token | None = None

    def activate(self) -> None:
        if self._token is None:
            self._token = _CURRENT_TRACE.set(self.trace)

    def close(self) -> None:
        if self._token is not None:
            _CURRENT_TRACE.reset(self._token)
            self._token = None


@dataclass
class HarnessDynamicContextController:
    trace: HarnessTraceContext
    origin_refs: dict[str, Any] = field(default_factory=dict)
    worktree_state: dict[str, Any] = field(default_factory=dict)
    git_state: dict[str, Any] = field(default_factory=dict)
    current_attempt: int = 1
    assigned_validation: dict[str, Any] = field(default_factory=dict)
    previous_validation_summary: str = ""
    recovery_constraints: tuple[str, ...] = ()
    source_revision: int | None = None
    source_hash: str = ""
    last_envelope_hash: str = ""
    injection_count: int = 0

    def update(self, **values: Any) -> None:
        allowed = {
            "origin_refs",
            "worktree_state",
            "git_state",
            "current_attempt",
            "assigned_validation",
            "previous_validation_summary",
            "recovery_constraints",
            "source_revision",
            "source_hash",
        }
        unknown = set(values).difference(allowed)
        if unknown:
            raise ValueError(f"Unknown Harness dynamic context field: {sorted(unknown)[0]}")
        for key, value in values.items():
            setattr(self, key, value)

    def envelope(self) -> EphemeralHarnessContextEnvelope:
        return EphemeralHarnessContextEnvelope(
            trace_id=self.trace.trace_id,
            trigger=self.trace.trigger,
            target=self.trace.target,
            invocation_id=self.trace.invocation_id,
            origin_refs=dict(self.origin_refs),
            worktree_state=dict(self.worktree_state),
            git_state=dict(self.git_state),
            current_attempt=self.current_attempt,
            assigned_validation=dict(self.assigned_validation),
            previous_validation_summary=self.previous_validation_summary,
            recovery_constraints=tuple(self.recovery_constraints),
            source_revision=self.source_revision,
            source_hash=self.source_hash,
        )

    def render_provider_query(self, original_query: str) -> str:
        if EPHEMERAL_CONTEXT_OPEN in original_query or EPHEMERAL_CONTEXT_CLOSE in original_query:
            raise ValueError("The persisted user query contains a reserved Harness context marker")
        envelope = self.envelope()
        self.last_envelope_hash = envelope.digest
        self.injection_count += 1
        return (
            f"<user_query>\n{original_query}\n</user_query>\n\n"
            f"{EPHEMERAL_CONTEXT_OPEN}\n{envelope.canonical_payload()}\n"
            f"{EPHEMERAL_CONTEXT_CLOSE}"
        )


def register_harness_context_callbacks(
    registry: HookRegistry,
    controller: HarnessDynamicContextController,
) -> HarnessTraceContextScope:
    """Inject the current envelope after Memory rebuilds messages but before Provider I/O."""

    scope = HarnessTraceContextScope(controller.trace)

    async def start_trace(event: HookEvent) -> None:
        del event
        scope.activate()

    async def inject(event: HookEvent) -> None:
        if not event.data.get("first_model_call"):
            return

        def render_ephemeral_context(messages: list[dict[str, Any]]) -> None:
            if not messages:
                raise ValueError("Harness context injection requires model messages")
            current = messages[-1]
            if not isinstance(current, dict) or current.get("role") != "user":
                raise ValueError("Harness context injection requires the current user query at the tail")
            original = current.get("content")
            if not isinstance(original, str):
                raise ValueError("Harness user query must be text")
            current["content"] = controller.render_provider_query(original)

        event.data["render_ephemeral_context"] = render_ephemeral_context
        event.data["ephemeral_context_hash_provider"] = lambda: controller.last_envelope_hash

    async def end_trace(event: HookEvent) -> None:
        del event
        scope.close()

    registry.register(HookPoint.TRACE_START, start_trace, priority=-150)
    registry.register(HookPoint.MODEL_BEFORE, inject, priority=-50)
    registry.register(HookPoint.TRACE_END, end_trace, priority=150)
    return scope
