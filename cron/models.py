"""Cron、Heartbeat 与持久化状态的严格数据契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tzlocal import get_localzone_name


ScheduleKind = Literal["interval", "once", "cron"]
JobState = Literal["scheduled", "paused", "completed"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    selected = value or utc_now()
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return selected.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"时间不是合法 ISO 8601：{value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区")
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
                raise ValueError("interval 计划只能设置 interval_seconds")
        elif self.kind == "once":
            if self.run_at is None or self.interval_seconds is not None or self.expression is not None:
                raise ValueError("once 计划只能设置 run_at")
            parse_time(self.run_at)
        elif self.kind == "cron":
            if not self.expression or self.interval_seconds is not None or self.run_at is not None:
                raise ValueError("cron 计划只能设置 expression")
        return self


class HeartbeatState(BaseModel):
    model_config = ConfigDict(validate_assignment=True, strict=True, extra="forbid")

    status: Literal["stopped", "running", "unhealthy"] = "stopped"
    interval_seconds: int = Field(default=60, ge=5)
    last_tick_at: str | None = None
    next_tick_at: str | None = None
    last_error: str | None = None


class CronJob(BaseModel):
    model_config = ConfigDict(validate_assignment=True, strict=True, extra="forbid")

    job_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    schedule: CronSchedule
    state: JobState = "scheduled"
    created_at: str
    updated_at: str
    next_run_at: str | None
    active_run_id: str | None = None
    manual_run_requested: bool = False
    last_scheduled_at: str | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_run_id: str | None = None
    last_session_id: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    run_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    skipped_overlap_count: int = Field(default=0, ge=0)


class CronState(BaseModel):
    model_config = ConfigDict(validate_assignment=True, strict=True, extra="forbid")

    version: Literal[1] = 1
    heartbeat: HeartbeatState = Field(default_factory=HeartbeatState)
    jobs: dict[str, CronJob] = Field(default_factory=dict)


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


class CronJobEditRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=20_000)
    schedule: CronSchedule | None = None


class CronPreviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schedule: CronSchedule
    count: int = Field(default=5, ge=1, le=20)
