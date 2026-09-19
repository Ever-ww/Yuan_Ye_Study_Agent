"""Gateway 内部 Dream Heartbeat 与缺失日期补跑。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from tzlocal import get_localzone_name

from .models import DreamRunResult, DreamStatus
from .service import DreamService
from backup import AgentHomeWriteGate, QuiesceResult


IdleCheck = Callable[[], bool]
ResultCallback = Callable[[DreamRunResult, bool], Awaitable[None]]
DayRunner = Callable[[date], Awaitable[DreamRunResult]]
HarnessDayRunner = Callable[[date], Awaitable[Any]]
CheckpointDayRunner = Callable[[date], Awaitable[Any]]


class DreamScheduler:
    """只在完整自然日到期且普通 Runtime 空闲时执行 Dream。"""

    def __init__(
        self,
        service: DreamService,
        is_idle: IdleCheck,
        on_result: ResultCallback,
        *,
        heartbeat_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
        run_day: DayRunner | None = None,
        run_harness_day: HarnessDayRunner | None = None,
        run_checkpoint_day: CheckpointDayRunner | None = None,
        write_gate: AgentHomeWriteGate | None = None,
    ) -> None:
        self.service = service
        self.is_idle = is_idle
        self.on_result = on_result
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.run_day = run_day or self.service.process_day
        self.run_harness_day = run_harness_day
        self.run_checkpoint_day = run_checkpoint_day
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._tick_lock = asyncio.Lock()
        self.last_error: str | None = None
        self._maintenance_epoch: int | None = None
        self._active_ticks: set[asyncio.Task[DreamRunResult | None]] = set()
        self.write_gate = write_gate

    async def start(self) -> None:
        if self._task is not None:
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="gateway-dream-heartbeat")

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await task

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> DreamRunResult | None:
        async def execute() -> DreamRunResult | None:
            if self.write_gate is not None:
                async with self.write_gate.operation(
                    "dream", f"tick:{self._local_now().isoformat()}",
                ):
                    return await self._tick_impl()
            return await self._tick_impl()

        task = asyncio.create_task(execute(), name="gateway-dream-tick")
        self._active_ticks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            # A participant pre-drain cancellation is an expected cooperative
            # pause.  Parent cancellation (Gateway close/caller cancellation)
            # must retain normal asyncio propagation semantics.
            if (
                self._maintenance_epoch is not None
                and not self._closing
                and task.cancelled()
            ):
                return None
            raise
        finally:
            self._active_ticks.discard(task)

    async def _tick_impl(self) -> DreamRunResult | None:
        async with self._tick_lock:
            if self._maintenance_epoch is not None:
                return None
            profile_enabled = self.service.config.dream_enabled
            harness_enabled = bool(
                self.run_harness_day is not None
                and self.service.config.harness_dream_enabled
            )
            checkpoint_enabled = bool(profile_enabled and self.run_checkpoint_day is not None)
            if (not profile_enabled and not harness_enabled and not checkpoint_enabled) or not self.is_idle():
                return None
            now = self._local_now()
            due = self._due_date(now) if profile_enabled else None
            profile_cycle_due = due is not None
            scheduled_day = self._latest_scheduled_day(now)
            if due is None and not harness_enabled:
                return None
            result: DreamRunResult | None = None
            if due is not None:
                try:
                    preflight = getattr(self.service, "advance_if_no_pending", None)
                    no_work = await preflight(due) if callable(preflight) else None
                    if no_work is not None:
                        # Cursor-only scans are deliberately invisible: no Run,
                        # Operation, Inbox row or model Runtime is created.
                        result = no_work
                    else:
                        result = await self.run_day(due)
                        await self._finish_durable_result(result)
                except Exception as exc:
                    self.last_error = str(exc) or type(exc).__name__
                else:
                    if result.status == "failed":
                        self.last_error = result.message
                    else:
                        self.last_error = None
            # 代码类Dream阶段拥有独立持久状态；Profile失败不能阻止同一tick中的独立阶段。
            if (
                checkpoint_enabled and self.run_checkpoint_day is not None
                and scheduled_day is not None and profile_cycle_due
            ):
                try:
                    await self.run_checkpoint_day(scheduled_day)
                except Exception as exc:
                    self.last_error = str(exc) or type(exc).__name__
            if harness_enabled and self.run_harness_day is not None and scheduled_day is not None:
                try:
                    await self.run_harness_day(scheduled_day)
                except Exception as exc:
                    self.last_error = str(exc) or type(exc).__name__
            return result

    async def _finish_durable_result(self, result: DreamRunResult) -> None:
        """Do not abandon Run finalization after Dream facts have committed."""
        run_id = str(getattr(result, "run_id", "unknown"))
        finishing = asyncio.create_task(
            self.on_result(result, True),
            name=f"gateway-dream-finalize-{run_id}",
        )
        try:
            await asyncio.shield(finishing)
        except asyncio.CancelledError:
            # Once DreamService returned, its canonical Memory transaction may
            # already be committed. Keep the lease until the matching Gateway
            # Run/event projection reaches its durable terminal boundary.
            await finishing
            raise

    async def prepare_quiesce(self, maintenance_epoch: int) -> None:
        """Stop active Dream model work before the global lease drain."""
        if self._maintenance_epoch is not None and maintenance_epoch < self._maintenance_epoch:
            return
        self._maintenance_epoch = maintenance_epoch
        self._wake.set()
        for task in tuple(self._active_ticks):
            if not task.done():
                task.cancel()

    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult:
        if self._maintenance_epoch is not None and maintenance_epoch < self._maintenance_epoch:
            return QuiesceResult(participant="dream", maintenance_epoch=maintenance_epoch,
                                  acknowledged=False, stale=True)
        await self.prepare_quiesce(maintenance_epoch)
        if self._active_ticks:
            await asyncio.gather(*tuple(self._active_ticks), return_exceptions=True)
        async with self._tick_lock:
            pass
        return QuiesceResult(participant="dream", maintenance_epoch=maintenance_epoch,
                              acknowledged=True,
                              safe_boundary="dream_interrupted_or_day_run_persisted")

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None
            self._wake.set()

    def status(self) -> DreamStatus:
        status = self.service.status(next_run_at=self._next_run_at())
        # Runner/finalization failures happen outside DreamService and used to
        # be invisible through the status API.
        if self.last_error:
            return status.model_copy(update={
                "last_status": "failed",
                "last_error": self.last_error,
            })
        return status

    def _due_date(self, now: datetime) -> date | None:
        state = self.service.status()
        if state.last_completed_date is None:
            # 第一次启用不回溯全部历史，只处理最近到期的一天。
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
            today_run = croniter(self.service.config.dream_schedule, start).get_next(datetime)
            due = now.date() - timedelta(days=1) if today_run <= now else None
        else:
            previous = croniter(self.service.config.dream_schedule, now).get_prev(datetime)
            due = previous.date() - timedelta(days=1)
            last = date.fromisoformat(state.last_completed_date)
            if last >= due:
                return None
        # Automatic Dream is one incremental changeset, not one Run per missed
        # calendar day.  All unconsumed Evidence through this cutoff is handled
        # by one memoryless execution.  Explicit backfill remains date-based.
        if (
            due is not None
            and getattr(state, "last_status", None) == "failed"
            and getattr(state, "last_attempted_date", None) == due.isoformat()
        ):
            return None
        return due

    def _next_run_at(self) -> str:
        now = self._local_now()
        return croniter(self.service.config.dream_schedule, now).get_next(datetime).isoformat(
            timespec="seconds",
        )

    def _latest_scheduled_day(self, now: datetime) -> date | None:
        previous = croniter(self.service.config.dream_schedule, now).get_prev(datetime)
        return previous.date() - timedelta(days=1)

    def _local_now(self) -> datetime:
        selected = self.clock()
        zone_name = (
            get_localzone_name()
            if self.service.config.dream_timezone == "local"
            else self.service.config.dream_timezone
        )
        zone = ZoneInfo(zone_name)
        if selected.tzinfo is None:
            selected = selected.replace(tzinfo=zone)
        return selected.astimezone(zone)

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.heartbeat_seconds)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
