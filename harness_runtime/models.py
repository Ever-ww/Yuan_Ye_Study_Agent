"""Frozen contracts for an isolated Harness trace and its ephemeral query context."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from memory.persistence import EPHEMERAL_CONTEXT_CLOSE, EPHEMERAL_CONTEXT_OPEN

class HarnessRuntimeTrigger(str, Enum):
    MANUAL = "manual"
    ERROR = "error"
    CAPABILITY = "capability"
    DREAM = "dream"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class HarnessPromptProfile(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    protocol_version: Literal[1] = 1
    trigger: HarnessRuntimeTrigger
    base_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    serialization_version: Literal[1] = 1

    @property
    def cache_key(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class HarnessRuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    trigger: HarnessRuntimeTrigger
    resource_root: Path
    tool_roots: tuple[Path, ...]
    skill_roots: tuple[Path, ...]
    stable_instructions: str

    @field_validator("tool_roots", "skill_roots", mode="before")
    @classmethod
    def _tuple_paths(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class HarnessTraceContext(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    trace_id: str = Field(min_length=1)
    trigger: HarnessRuntimeTrigger
    target: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    prompt_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_epoch: int = Field(default=1, ge=1)
    created_at: datetime


class EphemeralHarnessContextEnvelope(BaseModel):
    """Current facts injected into one provider query, never into Session JSONL."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: Literal[1] = 1
    trace_id: str
    trigger: HarnessRuntimeTrigger
    target: str
    invocation_id: str
    origin_refs: dict[str, Any] = Field(default_factory=dict)
    worktree_state: dict[str, Any] = Field(default_factory=dict)
    git_state: dict[str, Any] = Field(default_factory=dict)
    current_attempt: int = Field(default=1, ge=1)
    assigned_validation: dict[str, Any] = Field(default_factory=dict)
    previous_validation_summary: str = ""
    recovery_constraints: tuple[str, ...] = ()
    source_revision: int | None = Field(default=None, ge=0)
    source_hash: str = ""

    @field_validator("recovery_constraints", mode="before")
    @classmethod
    def _tuple_constraints(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    def canonical_payload(self) -> str:
        return canonical_json(self.model_dump(mode="json", exclude_none=True))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()


class ManualTurnInput(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    task: str
    assigned_test: str = ""


class RepairFeedback(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    attempt: int = Field(ge=1)
    command: str = ""
    returncode: int | None = None
    summary: str = ""
