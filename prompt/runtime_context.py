"""Changed-only context updates with immutable, durable provider projections."""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timedelta
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from memory.persistence import AGENT_EPHEMERAL_CONTEXT_CLOSE, AGENT_EPHEMERAL_CONTEXT_OPEN
from memory.provider_context import ProviderContextRecord, context_baseline, prepare_context

DYNAMIC_CONTEXT_REFRESH_INTERVAL = timedelta(hours=2)

if TYPE_CHECKING:
    from Agent.config import RuntimeConfig
    from memory import MemoryStore


class AgentRuntimeContextEnvelope(BaseModel):
    """Current facts, persisted only as dedicated provider-context metadata."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    session_created_at: str
    session_segment: str
    workspace_root: str
    operating_system: str
    architecture: str
    python_version: str
    timezone: str
    current_time: str
    sandbox_mode: str
    sandbox_shell: str | None = None
    runtime_notice: str = ""
    profile_context: str = ""
    conversation_summary: str = ""
    origin_refs: dict[str, str] = Field(default_factory=dict)
    source_hashes: dict[str, str] = Field(default_factory=dict)

    def canonical_payload(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()


class ProviderContextFragmentRegistry:
    """Trace-local provider-only fragments, rendered after the stable prefix.

    The registry is intentionally in-memory. Every recovery-sensitive fact in a
    fragment must already exist in its canonical store before registration.
    """

    def __init__(self) -> None:
        self._fragments: dict[str, dict[str, str]] = {}
        self._once: dict[str, set[str]] = {}

    def set(self, session_id: str, name: str, content: str, *, once: bool = False) -> None:
        selected = content.strip()
        if selected:
            self._fragments.setdefault(session_id, {})[name] = selected
            if once:
                self._once.setdefault(session_id, set()).add(name)
        else:
            self.remove(session_id, name)

    def remove(self, session_id: str, name: str) -> None:
        self._fragments.get(session_id, {}).pop(name, None)
        self._once.get(session_id, set()).discard(name)

    def render(self, session_id: str, *, consume_once: bool) -> str:
        fragments = self._fragments.get(session_id, {})
        priority = {"memory": -70, "continuity": -60, "harness": -50}
        names = sorted(fragments, key=lambda name: (priority.get(name, 0), name))
        rendered = "\n\n".join(fragments[name] for name in names)
        if consume_once:
            for name in tuple(self._once.get(session_id, ())):
                self.remove(session_id, name)
        return rendered

    def clear(self, session_id: str) -> None:
        self._fragments.pop(session_id, None)
        self._once.pop(session_id, None)

    def snapshot(self, session_id: str) -> dict[str, str]:
        return dict(self._fragments.get(session_id, {}))


class AgentDynamicContextBuilder:
    def __init__(self, config: "RuntimeConfig", memory: "MemoryStore") -> None:
        self.config = config
        self.memory = memory
        self.sandbox_mode = "closed"
        self.sandbox_shell = None
        self.last_envelope_hash = ""
        self.injection_count = 0
        self.fragments = ProviderContextFragmentRegistry()

    def set_sandbox_mode(self, mode: str) -> None:
        self.sandbox_mode = mode

    def envelope(
        self,
        session_id: str,
        *,
        origin_refs: dict[str, str] | None = None,
        current_time: str | None = None,
    ) -> AgentRuntimeContextEnvelope:
        now = datetime.now().astimezone()
        return AgentRuntimeContextEnvelope(
            session_id=session_id,
            session_created_at=self.memory.session_created_at(session_id),
            session_segment=self.memory.active_path(session_id).name,
            workspace_root=str(self.config.workspace_root),
            operating_system=f"{platform.system()} {platform.release()}",
            architecture=platform.machine(),
            python_version=platform.python_version(),
            timezone=now.tzname() or str(now.tzinfo),
            current_time=current_time or now.isoformat(timespec="seconds"),
            sandbox_mode=self.sandbox_mode,
            sandbox_shell=self.sandbox_shell,
            runtime_notice=str(getattr(self.memory, "runtime_notice", "")).strip(),
            # Long-term memory and continuation summaries are separate Hook
            # fragments. Keeping them out of this generic envelope avoids an
            # unconditional full-profile read on every model request.
            profile_context="",
            conversation_summary="",
            # Run/Turn IDs are provenance, not changing semantic context. They
            # belong in the durable packet metadata, not every provider update.
            origin_refs={},
            source_hashes={
                "profile": hashlib.sha256(b"").hexdigest(),
                "summary": hashlib.sha256(b"").hexdigest(),
            },
        )

    def prepare(
        self, original_query: str, session_id: str, *, origin_refs=None,
        baseline: dict[str, str] | None = None,
        context_epoch: str | None = None,
    ) -> ProviderContextRecord:
        now = datetime.now().astimezone()
        current = self.fragments.snapshot(session_id)
        if baseline is None:
            reader = getattr(self.memory, "session_context_records", None)
            baseline = context_baseline(reader(session_id)) if callable(reader) else {}
        baseline_time = _agent_context_time(baseline.get("agent", ""))
        reuse_time = (
            baseline_time is not None
            and timedelta(0) <= now - baseline_time < DYNAMIC_CONTEXT_REFRESH_INTERVAL
        )
        selected_time = baseline_time.isoformat(timespec="seconds") if reuse_time else now.isoformat(timespec="seconds")
        envelope = self.envelope(session_id, current_time=selected_time)
        current["agent"] = _render_agent_envelope(envelope)

        # Any other context update creates a new visible context baseline. The
        # two-hour clock therefore starts at this update, including a segment
        # change caused by compaction. Non-time Agent facts (sandbox/notice)
        # follow the same rule.
        managed = {"memory", "continuity", "harness"}
        other_changed = any(
            current.get(key) != baseline.get(key)
            and not (key not in current and _is_withdrawal(baseline.get(key, "")))
            for key in managed
        )
        agent_changed = current["agent"] != baseline.get("agent")
        if reuse_time and (other_changed or agent_changed):
            envelope = self.envelope(session_id, current_time=now.isoformat(timespec="seconds"))
            current["agent"] = _render_agent_envelope(envelope)
        return prepare_context(
            original_query, context_epoch or self.memory.active_path(session_id).name,
            current, baseline, origin_refs,
        )

    def render(
        self,
        original_query: str,
        session_id: str,
        *,
        origin_refs: dict[str, str] | None = None,
        track: bool = True,
    ) -> str:
        packet = self.prepare(original_query, session_id, origin_refs=origin_refs)
        if track:
            self.last_envelope_hash = packet.content_hash
            self.injection_count += bool(packet.fragments)
        return packet.render(original_query)


def _render_agent_envelope(envelope: AgentRuntimeContextEnvelope) -> str:
    return (
        f"{AGENT_EPHEMERAL_CONTEXT_OPEN}\n{envelope.canonical_payload()}\n"
        f"{AGENT_EPHEMERAL_CONTEXT_CLOSE}"
    )


def _agent_context_time(fragment: str) -> datetime | None:
    if not fragment.startswith(AGENT_EPHEMERAL_CONTEXT_OPEN):
        return None
    try:
        payload = fragment[len(AGENT_EPHEMERAL_CONTEXT_OPEN):]
        payload = payload.removesuffix(AGENT_EPHEMERAL_CONTEXT_CLOSE).strip()
        value = json.loads(payload).get("current_time")
        selected = datetime.fromisoformat(value) if isinstance(value, str) else None
        return selected if selected is not None and selected.tzinfo is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        # Legacy date-only or malformed context is never trusted as a clock
        # anchor; the next query writes a fresh versioned snapshot.
        return None


def _is_withdrawal(fragment: str) -> bool:
    return '"active":false' in fragment
