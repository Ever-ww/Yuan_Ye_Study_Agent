"""Cron job management over the SQLite durable repository."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from .models import (
    CronDispatch, CronJob, CronJobCreateRequest, CronJobEditRequest, CronPreview,
    CronRuntimeProfile, CronSchedule, CronStatus, parse_time, utc_iso, utc_now,
)
from .schedule import CronScheduleCalculator
from .store import CronStore


_NEVER_PREAPPROVE = frozenset({
    "bash", "cronjob", "harness_evolve", "harness_capability", "sandbox_rollback",
    "skill_install", "subagent",
})


class CronService:
    def __init__(self, store: CronStore, calculator: CronScheduleCalculator | None = None) -> None:
        self.store = store
        self.calculator = calculator or CronScheduleCalculator()
        self._waker: Callable[[], None] | None = None

    def set_waker(self, callback: Callable[[], None]) -> None:
        self._waker = callback

    def wake(self) -> None:
        if self._waker:
            self._waker()

    async def ensure(self) -> None:
        await self.store.ensure()

    def validate_schedule(self, schedule: CronSchedule) -> CronSchedule:
        if schedule.kind == "cron":
            return self.calculator.validate(str(schedule.expression), schedule.timezone)
        if schedule.kind == "once":
            parse_time(str(schedule.run_at))
        return schedule

    def preview(self, schedule: CronSchedule, *, count: int = 5, base_time: datetime | None = None) -> CronPreview:
        return self.calculator.preview(self.validate_schedule(schedule), count=count, base_time=base_time)

    def _profile(self, request: CronJobCreateRequest | CronJobEditRequest, previous: CronJob | None = None) -> CronRuntimeProfile:
        profile = getattr(request, "runtime_profile", None)
        grants = getattr(request, "preapproved_tools", None)
        if profile is not None:
            _validate_preapproved_tools(profile.allowed_tools)
            _validate_preapproved_tools(profile.preapproved_tools)
            return profile
        if grants is not None:
            selected = _validate_preapproved_tools(tuple(grants))
            allowed = tuple(sorted(set(selected) | set(previous.runtime_profile.allowed_tools if previous else ())))
            return CronRuntimeProfile(
                allowed_tools=allowed,
                allowed_skills=previous.runtime_profile.allowed_skills if previous else (),
                sandbox_policy=previous.runtime_profile.sandbox_policy if previous else "read_only",
                preapproved_tools=selected,
                limits=previous.runtime_profile.limits if previous else CronRuntimeProfile().limits,
            )
        return previous.runtime_profile if previous else CronRuntimeProfile()

    async def create(self, request: CronJobCreateRequest) -> CronJob:
        await self.store.ensure()
        schedule = self.validate_schedule(request.schedule)
        now = utc_now()
        job = CronJob(
            job_id=f"cron_{uuid4().hex}", project_id=request.project_id, name=request.name.strip(),
            prompt=request.prompt.strip(), schedule=schedule, misfire_policy=request.misfire_policy,
            runtime_profile=self._profile(request), created_at=utc_iso(now), updated_at=utc_iso(now),
            next_run_at=utc_iso(self._initial_next(schedule, now)),
        )
        await self.store.save_job(job)
        self.wake()
        return job

    async def ensure_paper_research_preset(self, *, project_id: str, expression: str, timezone_name: str) -> CronJob:
        await self.store.ensure()
        digest = hashlib.sha256(f"{project_id}:paper-research".encode()).hexdigest()[:16]
        job_id = f"cron_paper_research_{digest}"
        try:
            return await self.get(job_id)
        except KeyError:
            pass
        schedule = self.validate_schedule(CronSchedule(kind="cron", expression=expression, timezone=timezone_name))
        now = utc_now()
        tools = ("paper_library_download", "paper_library_save", "reference_write")
        profile = CronRuntimeProfile(allowed_tools=tools, allowed_skills=("search-summary-paper",), preapproved_tools=tools)
        job = CronJob(
            job_id=job_id, project_id=project_id, name="定时论文调研",
            prompt="执行一次无记忆论文调研。读取研究方向，使用已授权工具和 search-summary-paper Skill；对单一来源失败使用候选来源继续。",
            schedule=schedule, runtime_profile=profile, created_at=utc_iso(now), updated_at=utc_iso(now),
            next_run_at=utc_iso(self._initial_next(schedule, now)),
        )
        await self.store.save_job(job)
        self.wake()
        return job

    async def tool_preapproved(self, job_id: str, tool_name: str) -> bool:
        try:
            job = await self.get(job_id)
        except KeyError:
            return False
        return tool_name in job.runtime_profile.preapproved_tools

    async def edit(self, job_id: str, request: CronJobEditRequest) -> CronJob:
        job = await self.get(job_id)
        if job.state == "deleted":
            raise RuntimeError("deleted Cron job cannot be edited")
        now = utc_now(); timestamp = utc_iso(now)
        values = request.model_dump(exclude_none=True)
        schedule_changed = "schedule" in values
        schedule = self.validate_schedule(request.schedule) if request.schedule else job.schedule
        state = job.state
        if schedule_changed and job.schedule.kind == "once" and job.state == "completed":
            state = "scheduled"
        updated = job.model_copy(update={
            "name": str(values.get("name", job.name)).strip(),
            "prompt": str(values.get("prompt", job.prompt)).strip(),
            "schedule": schedule,
            "misfire_policy": values.get("misfire_policy", job.misfire_policy),
            "runtime_profile": self._profile(request, job),
            "state": state,
            "next_run_at": utc_iso(self._initial_next(schedule, now)) if schedule_changed else job.next_run_at,
            "revision": job.revision + 1, "updated_at": timestamp,
        })
        await self.store.save_job(updated, expected_revision=job.revision)
        self.wake()
        return updated

    async def pause(self, job_id: str) -> CronJob:
        job = await self.get(job_id)
        if job.state in {"completed", "deleted"}:
            raise RuntimeError("completed or deleted Cron job cannot be paused")
        updated = job.model_copy(update={"state": "paused", "revision": job.revision + 1, "updated_at": utc_iso()})
        await self.store.save_job(updated, expected_revision=job.revision)
        return updated

    async def resume(self, job_id: str) -> CronJob:
        job = await self.get(job_id)
        if job.state == "deleted":
            raise RuntimeError("deleted Cron job cannot be resumed")
        if job.schedule.kind == "once" and job.state == "completed":
            raise RuntimeError("completed one-time Cron job cannot be resumed")
        now = utc_now()
        updated = job.model_copy(update={"state": "scheduled", "next_run_at": utc_iso(self._initial_next(job.schedule, now)),
                                          "revision": job.revision + 1, "updated_at": utc_iso(now)})
        await self.store.save_job(updated, expected_revision=job.revision)
        self.wake()
        return updated

    async def trigger(self, job_id: str) -> CronJob:
        job = await self.get(job_id)
        if job.state == "deleted" or job.current_dispatch_id:
            raise RuntimeError("Cron job already has an active dispatch or has been deleted")
        now = utc_iso()
        snapshot = job.model_dump(mode="json")
        request_hash = hashlib.sha256(f"run-now:{job.job_id}:{job.revision}:{now}".encode()).hexdigest()
        dispatch = CronDispatch(dispatch_id=f"cron_dispatch_{uuid4().hex}", job_id=job.job_id, job_revision=job.revision,
                                trigger="run_now", job_snapshot=snapshot, request_hash=request_hash, created_at=now)
        await self.store.due_materialize(job, [dispatch], job.next_run_at, now=now)
        self.wake()
        return await self.get(job_id)

    async def retry(self, dispatch_id: str) -> CronDispatch:
        original = await self.store.dispatch(dispatch_id)
        if original.status not in {"failed", "cancelled"}:
            raise RuntimeError("only a determined failed/cancelled dispatch can be retried")
        now = utc_iso()
        retry = CronDispatch(dispatch_id=f"cron_dispatch_{uuid4().hex}", job_id=original.job_id,
                             job_revision=original.job_revision, trigger="retry", retry_of_dispatch_id=original.dispatch_id,
                             job_snapshot=original.job_snapshot, request_hash=hashlib.sha256(f"retry:{original.dispatch_id}:{now}".encode()).hexdigest(), created_at=now)
        job = await self.get(original.job_id)
        await self.store.due_materialize(job, [retry], job.next_run_at, now=now)
        self.wake()
        return retry

    async def remove(self, job_id: str) -> CronJob:
        job = await self.get(job_id)
        if job.state == "deleted":
            return job
        now = utc_iso()
        updated = job.model_copy(update={"state": "deleted", "next_run_at": None, "deleted_at": now,
                                          "revision": job.revision + 1, "updated_at": now})
        await self.store.save_job(updated, expected_revision=job.revision)
        # Pending dispatches cannot have side effects, hence are safe to cancel.
        for dispatch in await self.store.dispatches(job_id, limit=10_000):
            if dispatch.status in {"pending", "claimed"}:
                await self.store.cancel_unbound(dispatch.dispatch_id, reason="job_deleted_before_claim")
        self.wake()
        return updated

    async def list(self, project_id: str | None = None) -> tuple[CronJob, ...]:
        await self.store.ensure()
        return await self.store.jobs(project_id)

    async def get(self, job_id: str) -> CronJob:
        await self.store.ensure()
        return await self.store.job(job_id)

    async def history(self, job_id: str, *, limit: int = 100) -> tuple[CronDispatch, ...]:
        await self.store.ensure()
        return await self.store.dispatches(job_id, limit=limit)

    async def status(self, *, error: str | None = None) -> CronStatus:
        await self.store.ensure()
        heartbeat = await self.store.heartbeat()
        jobs = await self.store.jobs(include_deleted=True)
        return CronStatus(healthy=heartbeat.status != "unhealthy" and not error and not heartbeat.last_error,
                          heartbeat=heartbeat, jobs_total=len(jobs), jobs_scheduled=sum(j.state == "scheduled" for j in jobs),
                          jobs_running=sum(j.current_run_id is not None for j in jobs), last_error=error or heartbeat.last_error)

    async def project_has_jobs(self, project_id: str) -> bool:
        return bool(await self.store.jobs(project_id))

    def _initial_next(self, schedule: CronSchedule, now: datetime) -> datetime:
        if schedule.kind == "once":
            selected = parse_time(str(schedule.run_at))
            if selected <= now.astimezone(timezone.utc):
                raise ValueError("one-time schedule must be in the future")
            return selected
        selected = self.calculator.next_after(schedule, now)
        if selected is None:
            raise ValueError("schedule has no next run")
        return selected


def _validate_preapproved_tools(values: tuple[str, ...]) -> tuple[str, ...]:
    selected = tuple(sorted({value.strip() for value in values if value.strip()}))
    blocked = _NEVER_PREAPPROVE.intersection(selected)
    if blocked:
        raise ValueError("Cron cannot preapprove: " + ", ".join(sorted(blocked)))
    return selected
