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
        write_gate: AgentHomeWriteGate | None = None,
    ) -> None:
        self.service = service
        self.is_idle = is_idle
        self.on_result = on_result
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.run_day = run_day or self.service.process_day
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._tick_lock = asyncio.Lock()
        self._retry_after: datetime | None = None
        self.last_error: str | None = None
        self._maintenance_epoch: int | None = None
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
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> DreamRunResult | None:
        if self.write_gate is not None:
            async with self.write_gate.operation("dream", f"tick:{self._local_now().isoformat()}"):
                return await self._tick_impl()
        return await self._tick_impl()

    async def _tick_impl(self) -> DreamRunResult | None:
        async with self._tick_lock:
            if self._maintenance_epoch is not None:
                return None
            if not self.service.config.dream_enabled or not self.is_idle():
                return None
            now = self._local_now()
            if self._retry_after is not None and now < self._retry_after:
                return None
            due = self._due_date(now)
            if due is None:
                return None
            result = await self.run_day(due)
            await self.on_result(result, True)
            if result.status == "failed":
                self.last_error = result.message
                self._retry_after = now + timedelta(seconds=max(300, self.heartbeat_seconds))
            else:
                self.last_error = None
                self._retry_after = None
            return result

    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult:
        if self._maintenance_epoch is not None and maintenance_epoch <= self._maintenance_epoch:
            return QuiesceResult(participant="dream", maintenance_epoch=maintenance_epoch,
                                  acknowledged=maintenance_epoch == self._maintenance_epoch,
                                  stale=maintenance_epoch < self._maintenance_epoch)
        self._maintenance_epoch = maintenance_epoch
        self._wake.set()
        async with self._tick_lock:
            pass
        return QuiesceResult(participant="dream", maintenance_epoch=maintenance_epoch,
                              acknowledged=True, safe_boundary="day_run_persisted")

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None
            self._wake.set()

    def status(self) -> DreamStatus:
        return self.service.status(next_run_at=self._next_run_at())

    def _due_date(self, now: datetime) -> date | None:
        state = self.service.status()
        if state.last_completed_date is None:
            # 第一次启用不回溯全部历史，只处理最近到期的一天。
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
            today_run = croniter(self.service.config.dream_schedule, start).get_next(datetime)
            return now.date() - timedelta(days=1) if today_run <= now else None
        previous = croniter(self.service.config.dream_schedule, now).get_prev(datetime)
        due = previous.date() - timedelta(days=1)
        last = date.fromisoformat(state.last_completed_date)
        candidate = last + timedelta(days=1)
        return candidate if candidate <= due else None

    def _next_run_at(self) -> str:
        now = self._local_now()
        return croniter(self.service.config.dream_schedule, now).get_next(datetime).isoformat(
            timespec="seconds",
        )

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
            except TimeoutError:
                pass
