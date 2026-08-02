"""Cron 管理服务：只负责定义、校验与持久化，不负责执行模型。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from .models import (
    CronJob,
    CronJobCreateRequest,
    CronJobEditRequest,
    CronPreview,
    CronSchedule,
    CronStatus,
    parse_time,
    utc_iso,
    utc_now,
)
from .schedule import CronScheduleCalculator
from .store import CronStore


class CronService:
    def __init__(self, store: CronStore, calculator: CronScheduleCalculator | None = None) -> None:
        self.store = store
        self.calculator = calculator or CronScheduleCalculator()
        self._waker: Callable[[], None] | None = None

    def set_waker(self, callback: Callable[[], None]) -> None:
        self._waker = callback

    def wake(self) -> None:
        if self._waker is not None:
            self._waker()

    async def ensure(self) -> None:
        await self.store.ensure()

    def validate_schedule(self, schedule: CronSchedule) -> CronSchedule:
        if schedule.kind == "cron":
            return self.calculator.validate(str(schedule.expression), schedule.timezone)
        if schedule.kind == "once":
            parse_time(str(schedule.run_at))
        return schedule

    def preview(
        self,
        schedule: CronSchedule,
        *,
        count: int = 5,
        base_time: datetime | None = None,
    ) -> CronPreview:
        selected = self.validate_schedule(schedule)
        return self.calculator.preview(selected, count=count, base_time=base_time)

    async def create(self, request: CronJobCreateRequest) -> CronJob:
        schedule = self.validate_schedule(request.schedule)
        now = utc_now()
        next_run = self._initial_next(schedule, now)
        job_id = f"cron_{uuid4().hex}"
        job = CronJob(
            job_id=job_id,
            project_id=request.project_id,
            name=request.name.strip(),
            prompt=request.prompt.strip(),
            schedule=schedule,
            created_at=utc_iso(now),
            updated_at=utc_iso(now),
            next_run_at=utc_iso(next_run),
        )

        def add(state) -> None:
            state.jobs[job_id] = job

        await self.store.mutate(add)
        self.wake()
        return job

    async def edit(self, job_id: str, request: CronJobEditRequest) -> CronJob:
        selected: CronJob | None = None
        now = utc_now()

        def update(state) -> None:
            nonlocal selected
            job = _job(state.jobs, job_id)
            if job.active_run_id or job.manual_run_requested:
                raise RuntimeError("活动 Cron Job 不能编辑，请等待当前运行结束")
            values = request.model_dump(exclude_none=True)
            if "schedule" in values:
                schedule = self.validate_schedule(request.schedule)  # type: ignore[arg-type]
                values["schedule"] = schedule
                values["next_run_at"] = utc_iso(self._initial_next(schedule, now))
                values["state"] = "scheduled"
            if "name" in values:
                values["name"] = str(values["name"]).strip()
            if "prompt" in values:
                values["prompt"] = str(values["prompt"]).strip()
            values["updated_at"] = utc_iso(now)
            selected = CronJob.model_validate(job.model_copy(update=values).model_dump(), strict=True)
            state.jobs[job_id] = selected

        await self.store.mutate(update)
        self.wake()
        assert selected is not None
        return selected

    async def pause(self, job_id: str) -> CronJob:
        return await self._set_state(job_id, "paused")

    async def resume(self, job_id: str) -> CronJob:
        selected: CronJob | None = None
        now = utc_now()

        def update(state) -> None:
            nonlocal selected
            job = _job(state.jobs, job_id)
            if job.schedule.kind == "once" and job.state == "completed":
                raise RuntimeError("已经完成的单次 Cron Job 不能恢复")
            next_value = parse_time(job.next_run_at) if job.next_run_at else None
            if next_value is None:
                next_value = self._initial_next(job.schedule, now)
            selected = job.model_copy(update={
                "state": "scheduled",
                "next_run_at": utc_iso(next_value),
                "updated_at": utc_iso(now),
            })
            state.jobs[job_id] = selected

        await self.store.mutate(update)
        self.wake()
        assert selected is not None
        return selected

    async def trigger(self, job_id: str) -> CronJob:
        selected: CronJob | None = None
        now = utc_now()

        def update(state) -> None:
            nonlocal selected
            job = _job(state.jobs, job_id)
            if job.active_run_id or job.manual_run_requested:
                raise RuntimeError("Cron Job 已有活动运行，不能重复触发")
            selected = job.model_copy(update={
                "manual_run_requested": True,
                "updated_at": utc_iso(now),
            })
            state.jobs[job_id] = selected

        await self.store.mutate(update)
        self.wake()
        assert selected is not None
        return selected

    async def remove(self, job_id: str) -> CronJob:
        selected: CronJob | None = None

        def remove_job(state) -> None:
            nonlocal selected
            selected = _job(state.jobs, job_id)
            if selected.active_run_id or selected.manual_run_requested:
                raise RuntimeError("活动 Cron Job 不能删除")
            state.jobs.pop(job_id)

        await self.store.mutate(remove_job)
        self.wake()
        assert selected is not None
        return selected

    async def list(self, project_id: str | None = None) -> tuple[CronJob, ...]:
        state = await self.store.load()
        values = [
            job for job in state.jobs.values()
            if project_id is None or job.project_id == project_id
        ]
        return tuple(sorted(values, key=lambda item: (item.name.casefold(), item.job_id)))

    async def get(self, job_id: str) -> CronJob:
        return _job((await self.store.load()).jobs, job_id)

    async def status(self, *, error: str | None = None) -> CronStatus:
        try:
            state = await self.store.load()
        except (OSError, ValueError) as exc:
            return CronStatus(healthy=False, last_error=error or str(exc))
        jobs = tuple(state.jobs.values())
        last_error = error or state.heartbeat.last_error
        return CronStatus(
            healthy=state.heartbeat.status != "unhealthy" and not last_error,
            heartbeat=state.heartbeat,
            jobs_total=len(jobs),
            jobs_scheduled=sum(job.state == "scheduled" for job in jobs),
            jobs_running=sum(job.active_run_id is not None for job in jobs),
            last_error=last_error,
        )

    async def project_has_jobs(self, project_id: str) -> bool:
        return any(
            job.project_id == project_id
            and (job.state != "completed" or job.active_run_id is not None or job.manual_run_requested)
            for job in (await self.store.load()).jobs.values()
        )

    async def _set_state(self, job_id: str, value: str) -> CronJob:
        selected: CronJob | None = None

        def update(state) -> None:
            nonlocal selected
            job = _job(state.jobs, job_id)
            if job.state == "completed":
                raise RuntimeError("已经完成的单次 Cron Job 不能暂停")
            selected = job.model_copy(update={"state": value, "updated_at": utc_iso()})
            state.jobs[job_id] = selected

        await self.store.mutate(update)
        self.wake()
        assert selected is not None
        return selected

    def _initial_next(self, schedule: CronSchedule, now: datetime) -> datetime:
        if schedule.kind == "once":
            selected = parse_time(str(schedule.run_at))
            if selected <= now.astimezone(timezone.utc):
                raise ValueError("单次计划时间必须晚于当前时间")
            return selected
        selected = self.calculator.next_after(schedule, now)
        if selected is None:
            raise ValueError("计划没有下一次执行时间")
        return selected


def _job(jobs: dict[str, CronJob], job_id: str) -> CronJob:
    try:
        return jobs[job_id]
    except KeyError as exc:
        raise KeyError(f"Cron Job 不存在：{job_id}") from exc
