"""Durable Cron dispatcher: materialize → claim lease → bind existing Run."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backup import AgentHomeWriteGate, QuiesceResult

from .models import CronDispatch, CronJob, HeartbeatState, parse_time, utc_iso, utc_now
from .schedule import CronScheduleCalculator
from .store import CronStore


SubmitDispatch = Callable[[CronDispatch], Awaitable[None]]
Clock = Callable[[], datetime]


class CronScheduler:
    def __init__(
        self, store: CronStore, submit_dispatch: SubmitDispatch, *, gateway_epoch: Callable[[], str],
        calculator: CronScheduleCalculator | None = None, clock: Clock = utc_now,
        write_gate: AgentHomeWriteGate | None = None,
    ) -> None:
        self.store, self.submit_dispatch = store, submit_dispatch
        self.gateway_epoch = gateway_epoch
        self.calculator, self.clock, self.write_gate = calculator or CronScheduleCalculator(), clock, write_gate
        self._wake = asyncio.Event(); self._task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock(); self._closing = False; self.last_error: str | None = None
        self._maintenance_epoch: int | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.store.ensure()
        now = self._now()
        await self.store.set_heartbeat(HeartbeatState(status="running", interval_seconds=self.store.heartbeat_seconds,
                                                      next_tick_at=utc_iso(now)))
        self._closing = False; self._task = asyncio.create_task(self._run(), name="gateway-cron-heartbeat")

    async def close(self) -> None:
        self._closing = True; self._wake.set()
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            heartbeat = await self.store.heartbeat()
            await self.store.set_heartbeat(heartbeat.model_copy(update={"status": "stopped", "next_tick_at": None}))

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> tuple[str, ...]:
        if self.write_gate:
            async with self.write_gate.operation("cron", f"tick:{self._now().isoformat()}"):
                return await self._tick_impl()
        return await self._tick_impl()

    async def _tick_impl(self) -> tuple[str, ...]:
        async with self._tick_lock:
            if self._maintenance_epoch is not None:
                return ()
            now = self._now(); now_s = utc_iso(now)
            jobs = await self.store.jobs()
            for job in jobs:
                await self._materialize_due(job, now)
            claimed: list[CronDispatch] = []
            for job in await self.store.jobs():
                try:
                    dispatch = await self.store.claim_next(job.job_id, epoch=self.gateway_epoch())
                except Exception:
                    continue
                if dispatch:
                    claimed.append(dispatch)
            heartbeat = await self.store.heartbeat()
            await self.store.set_heartbeat(heartbeat.model_copy(update={"status": "running", "last_tick_at": now_s,
                                                                          "next_tick_at": utc_iso(now + timedelta(seconds=heartbeat.interval_seconds)),
                                                                          "last_error": None}))
        submitted: list[str] = []
        for dispatch in claimed:
            try:
                await self.submit_dispatch(dispatch)
                # Public scheduler callers historically received a Run identity.
                # Keep that compatibility while the Dispatch remains the durable
                # scheduling-domain identity.
                bound = await self.store.dispatch(dispatch.dispatch_id)
                submitted.append(bound.run_id or dispatch.dispatch_id)
            except Exception as exc:
                # The durable claim remains evidence. Reconcile may bind it; do not fabricate failure.
                self.last_error = str(exc) or type(exc).__name__
        return tuple(submitted)

    async def _materialize_due(self, job: CronJob, now: datetime) -> None:
        if job.state != "scheduled" or not job.next_run_at:
            return
        try:
            due = parse_time(job.next_run_at)
        except ValueError:
            return
        if due > now:
            return
        pending = sum(item.status == "pending" for item in await self.store.dispatches(job.job_id, limit=1000))
        occurrences: list[datetime] = []
        cursor = due
        # Bound iteration guards malformed very-high-frequency schedules.
        for _ in range(100_000):
            if cursor > now:
                break
            occurrences.append(cursor)
            nxt = self.calculator.next_after(job.schedule, cursor)
            if nxt is None:
                break
            cursor = nxt
        if not occurrences:
            return
        created: list[CronDispatch] = []
        snapshot = job.model_dump(mode="json")
        if job.current_dispatch_id or job.current_run_id:
            # Non-overlapping jobs record a skipped scheduling fact instead of
            # leaving a latent PENDING dispatch behind the active cycle.
            skipped = self._new_dispatch(
                job,
                "scheduled",
                snapshot,
                scheduled_for=utc_iso(occurrences[-1]),
                coalesced=len(occurrences),
                now=utc_iso(now),
            )
            created.append(skipped.model_copy(update={
                "status": "skipped",
                "completed_at": utc_iso(now),
                "error": "overlap_skipped",
            }))
            next_run = utc_iso(cursor) if cursor > now else None
        elif job.misfire_policy == "skip":
            skipped = self._new_dispatch(job, "scheduled", snapshot, scheduled_for=utc_iso(occurrences[-1]),
                                         coalesced=len(occurrences), now=utc_iso(now))
            created.append(skipped.model_copy(update={"status": "skipped", "completed_at": utc_iso(now), "error": "misfire_skip"}))
            next_run = utc_iso(cursor) if cursor > now else None
        elif job.misfire_policy == "fire_once":
            created.append(self._new_dispatch(job, "scheduled", snapshot, scheduled_for=utc_iso(occurrences[-1]),
                                              coalesced=len(occurrences), now=utc_iso(now)))
            next_run = utc_iso(cursor) if cursor > now else None
        else:
            capacity = max(0, 10 - pending)
            selected = occurrences[:capacity]
            for occurrence in selected:
                created.append(self._new_dispatch(job, "scheduled", snapshot, scheduled_for=utc_iso(occurrence), coalesced=1, now=utc_iso(now)))
            if len(selected) == len(occurrences):
                next_run = utc_iso(cursor) if cursor > now else None
            else:
                # next_run_at stays the oldest unmaterialized occurrence: durable CATCH_UP cursor.
                next_run = utc_iso(occurrences[len(selected)])
        if created:
            await self.store.due_materialize(job, created, next_run, now=utc_iso(now))

    @staticmethod
    def _new_dispatch(job: CronJob, trigger: str, snapshot: dict, *, scheduled_for: str, coalesced: int, now: str) -> CronDispatch:
        request_hash = hashlib.sha256(f"{job.job_id}:{job.revision}:{scheduled_for}:{coalesced}".encode()).hexdigest()
        return CronDispatch(dispatch_id=f"cron_dispatch_{uuid4().hex}", job_id=job.job_id, job_revision=job.revision,
                            trigger=trigger, scheduled_for=scheduled_for, coalesced_count=coalesced,
                            misfire_window_start=scheduled_for if coalesced == 1 else None,
                            misfire_window_end=scheduled_for, job_snapshot=snapshot, request_hash=request_hash, created_at=now)

    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult:
        if self._maintenance_epoch is not None and maintenance_epoch <= self._maintenance_epoch:
            return QuiesceResult(participant="cron", maintenance_epoch=maintenance_epoch,
                                 acknowledged=maintenance_epoch == self._maintenance_epoch, stale=maintenance_epoch < self._maintenance_epoch)
        self._maintenance_epoch = maintenance_epoch; self._wake.set()
        async with self._tick_lock:
            pass
        return QuiesceResult(participant="cron", maintenance_epoch=maintenance_epoch, acknowledged=True,
                             safe_boundary="tick_durable_claims_only")

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None; self._wake.set()

    async def _run(self) -> None:
        while not self._closing:
            try:
                state = await self.store.heartbeat()
                next_tick = parse_time(state.next_tick_at) if state.next_tick_at else self._now()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, (next_tick - self._now()).total_seconds()))
                    self._wake.clear()
                except TimeoutError:
                    pass
                if not self._closing:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
                heartbeat = await self.store.heartbeat()
                await self.store.set_heartbeat(heartbeat.model_copy(update={"status": "unhealthy", "last_error": self.last_error}))
                await asyncio.sleep(max(1, self.store.heartbeat_seconds))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Cron clock must be timezone-aware")
        return value.astimezone(timezone.utc)
