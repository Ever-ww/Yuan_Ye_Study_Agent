"""Canonical long-term memory models.

Conversation JSONL remains the short-term transcript.  The models in this
module describe durable, independently evidenced facts that may be recalled by
runtime hooks.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryScope(str, Enum):
    USER = "user"
    PROJECT = "project"
    SESSION = "session"
    RUN = "run"
    HARNESS = "harness"


class MemoryCardinality(str, Enum):
    SINGLE = "single"
    MULTI = "multi"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CONFLICTED = "conflicted"
    EXPIRED = "expired"
    TOMBSTONED = "tombstoned"


MemorySource = Literal[
    "explicit_user", "system_observed", "dream_consolidated",
    "model_inferred", "legacy_import", "compression", "harness_verified",
]


def normalize_memory_content(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def content_digest(value: str) -> str:
    return hashlib.sha256(normalize_memory_content(value).encode("utf-8")).hexdigest()


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    memory_id: str = Field(min_length=1)
    scope: MemoryScope
    scope_key: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=100)
    subject_key: str | None = Field(default=None, max_length=300)
    cardinality: MemoryCardinality = MemoryCardinality.MULTI
    content: str = Field(min_length=1, max_length=20000)
    normalized_content: str = Field(min_length=1)
    source: MemorySource
    primary_source_ref: str = Field(min_length=1, max_length=1000)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    pinned: bool = False
    valid_from: str | None = None
    valid_until: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    supersedes: str | None = None
    superseded_by: str | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str


class MemoryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    evidence_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    source: MemorySource
    source_ref: str = Field(min_length=1, max_length=1000)
    source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    locator: str | None = Field(default=None, max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: str


class MemoryWriteRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    scope: MemoryScope
    scope_key: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=20000)
    source: MemorySource
    source_ref: str = Field(min_length=1, max_length=1000)
    subject_key: str | None = Field(default=None, max_length=300)
    cardinality: MemoryCardinality = MemoryCardinality.MULTI
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    pinned: bool = False
    valid_from: str | None = None
    valid_until: str | None = None
    replace_existing: bool = False
    source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    locator: str | None = Field(default=None, max_length=1000)

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("memory content cannot be empty")
        return value

    @model_validator(mode="after")
    def _single_subject_required(self) -> "MemoryWriteRequest":
        if self.replace_existing and (
            self.cardinality is not MemoryCardinality.SINGLE or not self.subject_key
        ):
            raise ValueError("replace_existing requires SINGLE cardinality and subject_key")
        return self


class MemoryRetrievalProfile(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    candidate_limit: int = Field(default=30, ge=0, le=500)
    recall_summaries: bool = False
    lexical_limit: int = Field(default=30, ge=0, le=500)
    semantic_limit: int = Field(default=30, ge=0, le=500)
    max_records: int = Field(default=10, ge=0, le=100)
    token_budget: int = Field(default=3000, ge=0, le=50000)
    minimum_relevance: float = Field(default=0.05, ge=0.0, le=1.0)
    near_duplicate_jaccard: float = Field(default=0.90, ge=0.0, le=1.0)
    near_duplicate_cosine: float = Field(default=0.95, ge=0.0, le=1.0)
    mmr_relevance_weight: float = Field(default=0.80, ge=0.0, le=1.0)
    mmr_diversity_weight: float = Field(default=0.20, ge=0.0, le=1.0)
    supersede_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)


class MemoryAccessSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    runtime_profile: Literal["interactive", "cron", "harness", "maintenance", "memoryless"]
    scopes: tuple[tuple[MemoryScope, str], ...] = ()
    allowed_kinds: tuple[str, ...] = ()
    profile: MemoryRetrievalProfile
    created_at: str


class RankedMemory(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    record: MemoryRecord
    lexical_normalized: float = Field(ge=0.0, le=1.0)
    semantic_normalized: float = Field(ge=0.0, le=1.0)
    recency_normalized: float = Field(ge=0.0, le=1.0)
    importance_normalized: float = Field(ge=0.0, le=1.0)
    confidence_normalized: float = Field(ge=0.0, le=1.0)
    scope_affinity_normalized: float = Field(ge=0.0, le=1.0)
    relevance: float = Field(ge=0.0, le=1.0)
    final_score: float = Field(ge=0.0, le=1.0)


class MemoryTurnSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    snapshot_id: str
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    store_watermark: int = Field(ge=0)
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected: tuple[RankedMemory, ...] = ()
    token_budget: int = Field(ge=0)
    used_tokens: int = Field(ge=0)
    degradation_reason: str | None = None
    created_at: str

    def canonical_payload(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
