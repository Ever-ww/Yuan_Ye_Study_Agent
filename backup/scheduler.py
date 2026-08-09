"""Gateway-owned daily automatic backup scheduler with one-shot catch-up."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from croniter import croniter
from tzlocal import get_localzone_name

from .maintenance import AgentHomeWriteGate
from .service import BackupService


BackupCallback = Callable[[str, dict[str, object]], Awaitable[None]]


class BackupScheduler:
    def __init__(
        self,
        service: BackupService,
        write_gate: AgentHomeWriteGate,
        *,
        enabled: bool,
        schedule: str,
        timezone: str,
        drain_timeout_seconds: int,
        on_result: BackupCallback | None = None,
        heartbeat_seconds: int = 60,
    ) -> None:
        self.service = service
        self.write_gate = write_gate
        self.enabled = enabled
        self.schedule = schedule
        self.timezone = timezone
        self.drain_timeout_seconds = drain_timeout_seconds
        self.on_result = on_result
        self.heartbeat_seconds = heartbeat_seconds
        self.state_path = service.agent_root / ".yy" / "backup" / "state.json"
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._tick_lock = asyncio.Lock()
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._closing = False
        if not self.state_path.exists():
            await self._write_state({
                "version": 1,
                "initialized_at": self._now().isoformat(),
                "last_successful_backup": None,
                "last_attempt_at": None,
                "last_status": None,
                "last_error": None,
            })
        self._task = asyncio.create_task(self._run(), name="gateway-backup-scheduler")

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> str | None:
        async with self._tick_lock:
            if not self.enabled or not self._is_due():
                return None
            now = self._now()
            try:
                record = await self.service.create(
                    kind="automatic",
                    drain_timeout_seconds=self.drain_timeout_seconds,
                )
            except ValueError as exc:
                status = "backup_skipped"
                self.last_error = str(exc)
                await self._write_state_result(now, status, self.last_error, None)
                await self._notify(status, {"message": self.last_error})
                return status
            except Exception as exc:
                status = "backup_failed"
                self.last_error = str(exc) or type(exc).__name__
                await self._write_state_result(now, status, self.last_error, None)
                await self._notify(status, {"message": self.last_error})
                return status
            self.last_error = None
            await self._write_state_result(now, "backup_completed", None, str(record.path))
            await self._notify("backup_completed", record.model_dump(mode="json"))
            return "backup_completed"

    def status(self) -> dict[str, object]:
        return {
            **self._read_state(),
            "enabled": self.enabled,
            "schedule": self.schedule,
            "timezone": self.timezone,
            "next_run_at": croniter(self.schedule, self._now()).get_next(datetime).isoformat(),
        }

    def _is_due(self) -> bool:
        now = self._now()
        previous = croniter(self.schedule, now).get_prev(datetime)
        state = self._read_state()
        initialized = _parse(state.get("initialized_at"))
        successful = _parse(state.get("last_successful_backup"))
        attempted = _parse(state.get("last_attempt_at"))
        if initialized is not None and initialized > previous and successful is None:
            return False
        # At most one catch-up attempt for a missed scheduled point.
        return (successful is None or successful < previous) and (attempted is None or attempted < previous)

    async def _run(self) -> None:
        while not self._closing:
            await self.tick()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.heartbeat_seconds)
            except TimeoutError:
                pass

    async def _write_state_result(
        self,
        now: datetime,
        status: str,
        error: str | None,
        path: str | None,
    ) -> None:
        state = self._read_state()
        state.update({
            "last_attempt_at": now.isoformat(),
            "last_status": status,
            "last_error": error,
            "last_path": path,
        })
        if status == "backup_completed":
            state["last_successful_backup"] = now.isoformat()
        await self._write_state(state)

    async def _write_state(self, state: dict[str, object]) -> None:
        async with self.write_gate.operation("backup_scheduler", f"state:{uuid4().hex}"):
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".partial")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.state_path)

    def _read_state(self) -> dict[str, object]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def _notify(self, status: str, payload: dict[str, object]) -> None:
        if self.on_result is not None:
            await self.on_result(status, payload)

    def _now(self) -> datetime:
        zone = ZoneInfo(get_localzone_name() if self.timezone == "local" else self.timezone)
        return datetime.now(zone)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = ["BackupScheduler"]
