"""`.yy/cron/jobs.json` 的跨进程锁与原子读写。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from sandbox import WorkspaceLockManager

from .models import CronState, HeartbeatState


class CronStore:
    def __init__(self, agent_root: Path, *, heartbeat_seconds: int = 60) -> None:
        self.agent_root = agent_root.resolve()
        self.directory = self.agent_root / ".yy" / "cron"
        self.path = self.directory / "jobs.json"
        self.heartbeat_seconds = heartbeat_seconds
        self._locks = WorkspaceLockManager(self.agent_root, state_root=self.agent_root)

    async def ensure(self) -> CronState:
        async with self._locks.write(self.path):
            if self.path.exists():
                state = self._read_unlocked()
                if state.heartbeat.interval_seconds != self.heartbeat_seconds:
                    state.heartbeat.interval_seconds = self.heartbeat_seconds
                    self._write_unlocked(state)
                return state
            state = CronState(heartbeat=HeartbeatState(interval_seconds=self.heartbeat_seconds))
            self._write_unlocked(state)
            return state

    async def load(self) -> CronState:
        async with self._locks.read(self.path):
            if not self.path.exists():
                raise FileNotFoundError(f"Cron 状态文件不存在：{self.path}")
            return self._read_unlocked()

    async def save(self, state: CronState) -> CronState:
        selected = CronState.model_validate(state.model_dump(mode="python"), strict=True)
        async with self._locks.write(self.path):
            self._write_unlocked(selected)
        return selected

    async def mutate(self, callback: Callable[[CronState], None]) -> CronState:
        async with self._locks.write(self.path):
            state = self._read_unlocked() if self.path.exists() else CronState(
                heartbeat=HeartbeatState(interval_seconds=self.heartbeat_seconds),
            )
            callback(state)
            selected = CronState.model_validate(state.model_dump(mode="python"), strict=True)
            self._write_unlocked(selected)
            return selected

    def _read_unlocked(self) -> CronState:
        try:
            return CronState.model_validate_json(self.path.read_text(encoding="utf-8"), strict=True)
        except (OSError, ValidationError, ValueError) as exc:
            raise ValueError(f"Cron 状态文件损坏：{self.path}\n{exc}") from exc

    def _write_unlocked(self, state: CronState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".{self.path.name}.{uuid4().hex}.tmp"
        temporary.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(self.path)
