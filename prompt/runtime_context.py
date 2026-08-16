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


class AgentDynamicContextBuilder:
    def __init__(self, config: "RuntimeConfig", memory: "MemoryStore") -> None:
        self.config = config
        self.memory = memory
        self.sandbox_mode = "closed"
        self.last_envelope_hash = ""
        self.injection_count = 0

    def set_sandbox_mode(self, mode: str) -> None:
        self.sandbox_mode = mode

    def envelope(
        self,
        session_id: str,
        *,
        origin_refs: dict[str, str] | None = None,
    ) -> AgentRuntimeContextEnvelope:
        now = datetime.now().astimezone()
        profile = self.memory.prompt_context(session_id)
        summary = self.memory.latest_summary(session_id)
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
            runtime_notice=str(getattr(self.memory, "runtime_notice", "")).strip(),
            profile_context=profile,
            conversation_summary=summary,
            origin_refs=dict(sorted((origin_refs or {}).items())),
            source_hashes={
                "profile": hashlib.sha256(profile.encode("utf-8")).hexdigest(),
                "summary": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
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
        return (
            f"<user_query>\n{original_query}\n</user_query>\n\n"
            f"{AGENT_EPHEMERAL_CONTEXT_OPEN}\n{envelope.canonical_payload()}\n"
            f"{AGENT_EPHEMERAL_CONTEXT_CLOSE}"
        )
