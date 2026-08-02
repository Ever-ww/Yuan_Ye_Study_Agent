"""Gateway Heartbeat 与到期 Cron Job 的声明、提交和结算。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .models import CronJob, parse_time, utc_iso, utc_now
from .schedule import CronScheduleCalculator
from .store import CronStore


RunLookup = Callable[[str], Any]
SubmitRun = Callable[[CronJob, str], Awaitable[None]]
Clock = Callable[[], datetime]


class CronScheduler:
    def __init__(
        self,
        store: CronStore,
        submit_run: SubmitRun,
        run_lookup: RunLookup,
        *,
        calculator: CronScheduleCalculator | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.submit_run = submit_run
        self.run_lookup = run_lookup
        self.calculator = calculator or CronScheduleCalculator()
        self.clock = clock
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._closing = False
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        try:
            await self.store.ensure()
            now = self._now()

            def started(state) -> None:
                state.heartbeat.status = "running"
                state.heartbeat.last_error = None
                state.heartbeat.next_tick_at = utc_iso(now)

            await self.store.mutate(started)
            self.last_error = None
        except (OSError, ValueError) as exc:
            # Cron 状态损坏只隔离调度器；Gateway 聊天、Session 和工具主链仍要可用。
            self.last_error = str(exc) or type(exc).__name__
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="gateway-cron-heartbeat")

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await self.store.mutate(lambda state: _stop_heartbeat(state))
        except (OSError, ValueError):
            pass

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> tuple[str, ...]:
        async with self._tick_lock:
            now = self._now()
            claimed: list[tuple[CronJob, str]] = []

            def mutate(state) -> None:
                for job_id, job in tuple(state.jobs.items()):
                    job = self._reconcile(job, now)
                    if job.manual_run_requested and not job.active_run_id:
                        run_id = uuid4().hex
                        job = job.model_copy(update={
                            "manual_run_requested": False,
                            "active_run_id": run_id,
                            "last_run_id": run_id,
                            "last_scheduled_at": utc_iso(now),
                            "last_started_at": utc_iso(now),
                            "updated_at": utc_iso(now),
                        })
                        claimed.append((job, run_id))
                    elif job.state == "scheduled" and job.next_run_at:
                        due_at = parse_time(job.next_run_at)
                        if due_at <= now:
                            if job.active_run_id:
                                if job.schedule.kind != "once":
                                    next_value = self.calculator.next_future(job.schedule, due_at, now)
                                    job = job.model_copy(update={
                                        "next_run_at": utc_iso(next_value) if next_value else None,
                                        "skipped_overlap_count": job.skipped_overlap_count + 1,
                                        "updated_at": utc_iso(now),
                                    })
                            else:
                                run_id = uuid4().hex
                                next_value = self.calculator.next_future(job.schedule, due_at, now)
                                job = job.model_copy(update={
                                    "active_run_id": run_id,
                                    "last_run_id": run_id,
                                    "last_scheduled_at": utc_iso(due_at),
                                    "last_started_at": utc_iso(now),
                                    "next_run_at": utc_iso(next_value) if next_value else None,
                                    "updated_at": utc_iso(now),
                                })
                                claimed.append((job, run_id))
                    state.jobs[job_id] = job
                state.heartbeat.status = "running"
                state.heartbeat.last_tick_at = utc_iso(now)
                state.heartbeat.next_tick_at = utc_iso(
                    now + timedelta(seconds=state.heartbeat.interval_seconds),
                )
                state.heartbeat.last_error = None

            await self.store.mutate(mutate)
            self.last_error = None

        submitted: list[str] = []
        for job, run_id in claimed:
            try:
                await self.submit_run(job, run_id)
                submitted.append(run_id)
            except Exception as exc:
                await self._submission_failed(job, run_id, exc)
        return tuple(submitted)

    async def _run(self) -> None:
        while not self._closing:
            try:
                state = await self.store.load()
                next_tick = parse_time(state.heartbeat.next_tick_at) if state.heartbeat.next_tick_at else self._now()
                delay = max(0.0, (next_tick - self._now()).total_seconds())
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                    self._wake.clear()
                except TimeoutError:
                    pass
                if self._closing:
                    break
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
                try:
                    await self.store.mutate(lambda state: _unhealthy(state, self.last_error or "unknown"))
                except Exception:
                    pass
                await asyncio.sleep(max(1, self.store.heartbeat_seconds))

    def _reconcile(self, job: CronJob, now: datetime) -> CronJob:
        if not job.active_run_id:
            return job
        try:
            run = self.run_lookup(job.active_run_id)
        except KeyError:
            return self._finish_job(job, "interrupted", None, "Gateway 重启时未找到活动 Run", now)
        if run.status in {"queued", "running"}:
            return job
        finished = self._finish_job(
            job,
            run.status,
            getattr(run, "session_id", None),
            getattr(run, "error", None),
            now,
        )
        # 如果下一计划点发生在上一次运行尚未结束时，该周期按 overlap 跳过，绝不补排并行任务。
        finished_at = getattr(run, "finished_at", None)
        boundary = parse_time(finished_at) if finished_at else now
        if (
            finished.state == "scheduled"
            and finished.next_run_at
            and parse_time(finished.next_run_at) <= boundary
        ):
            due_at = parse_time(finished.next_run_at)
            next_value = self.calculator.next_future(finished.schedule, due_at, boundary)
            finished = finished.model_copy(update={
                "next_run_at": utc_iso(next_value) if next_value else None,
                "skipped_overlap_count": finished.skipped_overlap_count + 1,
            })
        return finished

    def _finish_job(
        self,
        job: CronJob,
        status: str,
        session_id: str | None,
        error: str | None,
        now: datetime,
    ) -> CronJob:
        success = status == "completed"
        state = "completed" if job.schedule.kind == "once" else job.state
        return job.model_copy(update={
            "active_run_id": None,
            "state": state,
            "last_finished_at": utc_iso(now),
            "last_session_id": session_id,
            "last_status": status,
            "last_error": error,
            "run_count": job.run_count + 1,
            "failure_count": job.failure_count + (0 if success else 1),
            "updated_at": utc_iso(now),
        })

    async def _submission_failed(self, job: CronJob, run_id: str, exc: Exception) -> None:
        now = self._now()

        def update(state) -> None:
            current = state.jobs.get(job.job_id)
            if current is None or current.active_run_id != run_id:
                return
            state.jobs[job.job_id] = self._finish_job(
                current, "failed", None, str(exc) or type(exc).__name__, now,
            )

        await self.store.mutate(update)

    def _now(self) -> datetime:
        selected = self.clock()
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise ValueError("Heartbeat 时钟必须包含时区")
        return selected.astimezone(timezone.utc)


def _stop_heartbeat(state) -> None:
    state.heartbeat.status = "stopped"
    state.heartbeat.next_tick_at = None


def _unhealthy(state, error: str) -> None:
    state.heartbeat.status = "unhealthy"
    state.heartbeat.last_error = error
