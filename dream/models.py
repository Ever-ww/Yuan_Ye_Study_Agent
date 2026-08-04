"""Dream 每日记忆巩固的严格数据契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DreamOperation = Literal["insert", "update", "supersede"]
DreamRunStatus = Literal["completed", "noop", "failed"]


class DreamEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_key: str = Field(min_length=1)
    session_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    source_file: str = Field(min_length=1)
    line_number: int = Field(ge=1)
    timestamp: str = Field(min_length=1)
    content: str = Field(min_length=1)


class DreamTranscriptRecord(BaseModel):
    """送入 Dream 的净化对话；只有 user 记录拥有 evidence_id。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)
    timestamp: str = Field(min_length=1)
    workspace_key: str = Field(min_length=1)
    session_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    source_file: str = Field(min_length=1)
    line_number: int = Field(ge=1)
    evidence_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence_owner(self) -> "DreamTranscriptRecord":
        if self.role == "user" and self.evidence_id is None:
            raise ValueError("user Dream 记录必须包含 evidence_id")
        if self.role == "assistant" and self.evidence_id is not None:
            raise ValueError("assistant 只能作为语境，不能拥有 evidence_id")
        return self


class DreamDayArchive(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    timezone: str = Field(min_length=1)
    records: tuple[DreamTranscriptRecord, ...] = ()
    evidence: tuple[DreamEvidence, ...] = ()
    session_count: int = Field(default=0, ge=0)
    source_file_count: int = Field(default=0, ge=0)


class DreamCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    target_file: str = Field(pattern=r"^[^/\\]+\.md$")
    statement: str = Field(min_length=1, max_length=2000)
    operation: DreamOperation = "insert"
    memory_id: str | None = Field(default=None, pattern=r"^mem_[0-9a-f]{16}$")
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_operation(self) -> "DreamCandidate":
        if self.operation in {"update", "supersede"} and self.memory_id is None:
            raise ValueError(f"{self.operation} 必须指定 memory_id")
        return self


class DreamCandidateList(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    candidates: tuple[DreamCandidate, ...] = ()


class DreamMemoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    memory_id: str = Field(pattern=r"^mem_[0-9a-f]{16}$")
    target_file: str = Field(pattern=r"^[^/\\]+\.md$")
    statement: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    first_seen_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    last_seen_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: Literal["active", "superseded"] = "active"
    superseded_by: str | None = Field(default=None, pattern=r"^mem_[0-9a-f]{16}$")
    run_id: str = Field(min_length=1)


class DreamMemoryIndex(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[1] = 1
    memories: dict[str, DreamMemoryEntry] = Field(default_factory=dict)


class DreamState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[1] = 1
    initialized_at: str
    last_completed_date: str | None = None
    processed_evidence: dict[str, list[str]] = Field(default_factory=dict)
    successful_runs: list[str] = Field(default_factory=list)
    last_run_id: str | None = None
    last_status: DreamRunStatus | None = None
    last_error: str | None = None


class DreamRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str = Field(min_length=1)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: DreamRunStatus
    message: str
    sessions_processed: int = Field(default=0, ge=0)
    source_files_processed: int = Field(default=0, ge=0)
    records_processed: int = Field(default=0, ge=0)
    evidence_processed: int = Field(default=0, ge=0)
    memories_changed: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model: str = ""
    created_at: str


class DreamRollbackResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    restored: bool
    message: str


class DreamStatus(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    enabled: bool
    running: bool
    schedule: str
    timezone: str
    initialized_at: str
    last_completed_date: str | None = None
    last_run_id: str | None = None
    last_status: DreamRunStatus | None = None
    last_error: str | None = None
    next_run_at: str | None = None


class DreamRunRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class DreamBackfillRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class DreamRollbackRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    run_id: str | None = Field(default=None, min_length=1)
