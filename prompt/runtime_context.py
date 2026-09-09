"""Provider-only dynamic context for the main Agent request tail."""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from memory.persistence import AGENT_EPHEMERAL_CONTEXT_CLOSE, AGENT_EPHEMERAL_CONTEXT_OPEN

if TYPE_CHECKING:
    from Agent.config import RuntimeConfig
    from memory import MemoryStore


class AgentRuntimeContextEnvelope(BaseModel):
    """Rebuildable current facts that must not enter the conversation Session."""

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
            current_time=now.isoformat(),
            sandbox_mode=self.sandbox_mode,
            sandbox_shell=self.sandbox_shell,
            runtime_notice=str(getattr(self.memory, "runtime_notice", "")).strip(),
            # Long-term memory and continuation summaries are separate Hook
            # fragments. Keeping them out of this generic envelope avoids an
            # unconditional full-profile read on every model request.
            profile_context="",
            conversation_summary="",
            origin_refs=dict(sorted((origin_refs or {}).items())),
            source_hashes={
                "profile": hashlib.sha256(b"").hexdigest(),
                "summary": hashlib.sha256(b"").hexdigest(),
            },
        )

    def render(
        self,
        original_query: str,
        session_id: str,
        *,
        origin_refs: dict[str, str] | None = None,
        track: bool = True,
    ) -> str:
        if AGENT_EPHEMERAL_CONTEXT_OPEN in original_query or AGENT_EPHEMERAL_CONTEXT_CLOSE in original_query:
            raise ValueError("The persisted user query contains a reserved Agent context marker")
        envelope = self.envelope(session_id, origin_refs=origin_refs)
        if track:
            self.last_envelope_hash = envelope.digest
            self.injection_count += 1
        fragments = self.fragments.render(session_id, consume_once=track)
        rendered = (
            f"<user_query>\n{original_query}\n</user_query>\n\n"
            f"{AGENT_EPHEMERAL_CONTEXT_OPEN}\n{envelope.canonical_payload()}\n"
            f"{AGENT_EPHEMERAL_CONTEXT_CLOSE}"
        )
        return rendered + (f"\n\n{fragments}" if fragments else "")
