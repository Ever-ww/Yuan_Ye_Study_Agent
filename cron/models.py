"""Cron durable contracts.  SQLite is authoritative; JSON is migration-only."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tzlocal import get_localzone_name


ScheduleKind = Literal["interval", "once", "cron"]
JobState = Literal["scheduled", "paused", "completed", "deleted"]
DispatchState = Literal[
    "pending", "claimed", "running", "succeeded", "failed", "cancelled", "skipped", "recovery_required",
]
MisfirePolicy = Literal["skip", "fire_once", "catch_up"]
SandboxPolicy = Literal["read_only", "checkpointed_workspace"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    selected = value or utc_now()
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("time must include timezone")
    return selected.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO 8601 timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time must include timezone")
    return parsed.astimezone(timezone.utc)


class CronSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    kind: ScheduleKind
    interval_seconds: int | None = Field(default=None, ge=60)
    run_at: str | None = None
    expression: str | None = None
    timezone: str = Field(default_factory=get_localzone_name, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_shape(self) -> "CronSchedule":
        if self.kind == "interval":
            if self.interval_seconds is None or self.run_at is not None or self.expression is not None:
                raise ValueError("interval schedule requires only interval_seconds")
        elif self.kind == "once":
            if self.run_at is None or self.interval_seconds is not None or self.expression is not None:
                raise ValueError("once schedule requires only run_at")
            parse_time(self.run_at)
        elif not self.expression or self.interval_seconds is not None or self.run_at is not None:
            raise ValueError("cron schedule requires only expression")
        return self


class CronResourceLimits(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    timeout_seconds: int = Field(default=1800, ge=30, le=86_400)
    max_turns: int = Field(default=1, ge=1, le=32)
    max_model_calls: int = Field(default=8, ge=1, le=200)
    max_tool_calls: int = Field(default=24, ge=0, le=500)
    token_budget: int = Field(default=200_000, ge=1_000, le=10_000_000)


class CronRuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    allowed_tools: tuple[str, ...] = ()
    allowed_skills: tuple[str, ...] = ()
    sandbox_policy: SandboxPolicy = "read_only"
    preapproved_tools: tuple[str, ...] = ()
    limits: CronResourceLimits = Field(default_factory=CronResourceLimits)

    @model_validator(mode="after")
    def validate_grants(self) -> "CronRuntimeProfile":
        if not set(self.preapproved_tools).issubset(self.allowed_tools):
            raise ValueError("preapproved_tools must be a subset of allowed_tools")
        return self


class HeartbeatState(BaseModel):
    model_config = ConfigDict(validate_assignment=True, strict=True, extra="forbid")
    status: Literal["stopped", "running", "unhealthy"] = "stopped"
    interval_seconds: int = Field(default=60, ge=5)
    last_tick_at: str | None = None
    next_tick_at: str | None = None
    last_error: str | None = None


class CronJob(BaseModel):
    """Durable job projection. Compatibility aliases are retained for old clients."""
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    job_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    schedule: CronSchedule
    state: JobState = "scheduled"
    misfire_policy: MisfirePolicy = "fire_once"
    runtime_profile: CronRuntimeProfile = Field(default_factory=CronRuntimeProfile)
    revision: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    next_run_at: str | None = None
    current_dispatch_id: str | None = None
    current_run_id: str | None = None
    last_run_at: str | None = None
    last_run_id: str | None = None
    last_session_id: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_success_at: str | None = None
    last_success_run_id: str | None = None
    last_failure_at: str | None = None
    last_failure_run_id: str | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    missed_count: int = Field(default=0, ge=0)
    overlap_skipped: int = Field(default=0, ge=0)
    run_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)

    @property
    def preapproved_tools(self) -> tuple[str, ...]:
        return self.runtime_profile.preapproved_tools

    @property
    def active_run_id(self) -> str | None:
        return self.current_run_id

    @property
    def skipped_overlap_count(self) -> int:
        return self.overlap_skipped


class CronDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    dispatch_id: str
    job_id: str
    job_revision: int = Field(ge=1)
    trigger: Literal["scheduled", "run_now", "retry", "legacy"]
    scheduled_for: str | None = None
    status: DispatchState = "pending"
    coalesced_count: int = Field(default=1, ge=1)
    misfire_window_start: str | None = None
    misfire_window_end: str | None = None
    claim_token: str | None = None
    claim_epoch: str | None = None
    claim_expires_at: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    retry_of_dispatch_id: str | None = None
    job_snapshot: dict
    request_hash: str
    revision: int = Field(default=1, ge=1)
    result: str | None = None
    error: str | None = None
    created_at: str
    claimed_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "CronDispatch":
        # A cancellation before a scheduler lease is bound to a Run is valid
        # (notably Job deletion). A cancelled dispatch may therefore be either
        # bound or unbound; the execution states below are bound facts.
        bound = self.status in {"running", "succeeded", "failed", "recovery_required"}
        if self.status == "claimed" and self.run_id is not None:
            raise ValueError("claimed dispatch cannot contain run_id")
        if bound and not all((self.session_id, self.run_id, self.operation_id, self.attempt_id)):
            raise ValueError("bound dispatch requires session/run/operation/attempt identity")
        return self


class CronPreview(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    schedule: CronSchedule
    next_runs: tuple[str, ...]


class CronStatus(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    healthy: bool
    heartbeat: HeartbeatState | None = None
    jobs_total: int = Field(default=0, ge=0)
    jobs_scheduled: int = Field(default=0, ge=0)
    jobs_running: int = Field(default=0, ge=0)
    last_error: str | None = None


class CronJobCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    schedule: CronSchedule
    misfire_policy: MisfirePolicy = "fire_once"
    runtime_profile: CronRuntimeProfile | None = None
    preapproved_tools: tuple[str, ...] = ()  # compatibility input


class CronJobEditRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=20_000)
    schedule: CronSchedule | None = None
    misfire_policy: MisfirePolicy | None = None
    runtime_profile: CronRuntimeProfile | None = None
    preapproved_tools: tuple[str, ...] | None = None


class CronPreviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    schedule: CronSchedule
    count: int = Field(default=5, ge=1, le=20)


class CronPaperResearchPresetRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    project_id: str = Field(min_length=1)
    expression: str = Field(default="0 9 * * 1", min_length=1, max_length=100)
    timezone: str = Field(default_factory=get_localzone_name, min_length=1, max_length=100)


# Kept solely so old imports remain valid during JSON migration.
class CronState(BaseModel):
    model_config = ConfigDict(validate_assignment=True, strict=True, extra="forbid")
    version: Literal[1] = 1
    heartbeat: HeartbeatState = Field(default_factory=HeartbeatState)
    jobs: dict[str, CronJob] = Field(default_factory=dict)
