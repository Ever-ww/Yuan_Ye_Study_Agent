"""Cooperative Agent Home mutation barrier and maintenance coordinator."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Protocol

from .models import MaintenanceSnapshot, MaintenanceState, QuiesceResult
from .control import ExternalControlLock


class MaintenanceBlockedError(RuntimeError):
    """A normal mutation was attempted while Agent Home was draining/frozen."""


class MaintenanceParticipant(Protocol):
    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult: ...

    async def resume(self, maintenance_epoch: int) -> None: ...


@dataclass(frozen=True)
class WriteScope:
    writer_id: str
    logical_operation_id: str
    maintenance_epoch: int | None
    maintenance: bool
    generation: int


_CURRENT_SCOPE: ContextVar[WriteScope | None] = ContextVar(
    "yy_agent_home_write_scope", default=None,
)


class AgentHomeWriteGate:
    """In-process admission barrier for all framework-owned Agent Home writes.

    Permits are deliberately not persisted. Durable recovery remains the job of
    the FSM/operation ledger and restore control plane.
    """

    def __init__(self) -> None:
        self._state = MaintenanceState.RUNNING
        self._epoch = 0
        self._generation = 0
        self._active: dict[int, WriteScope] = {}
        self._next_scope = 0
        self._condition = asyncio.Condition()

    @property
    def state(self) -> MaintenanceState:
        return self._state

    @property
    def maintenance_epoch(self) -> int:
        return self._epoch

    @property
    def current_scope(self) -> WriteScope | None:
        return _CURRENT_SCOPE.get()

    @asynccontextmanager
    async def operation(
        self,
        writer_id: str,
        logical_operation_id: str,
        *,
        maintenance_epoch: int | None = None,
    ) -> AsyncIterator[WriteScope]:
        maintenance = maintenance_epoch is not None
        async with self._condition:
            if maintenance:
                if self._state is not MaintenanceState.FROZEN or maintenance_epoch != self._epoch:
                    raise MaintenanceBlockedError("维护写入不属于当前 FROZEN epoch")
            elif self._state is not MaintenanceState.RUNNING:
                raise MaintenanceBlockedError(
                    f"Agent Home 当前为 {self._state.value}，新的写入已冻结",
                )
            self._next_scope += 1
            scope_id = self._next_scope
            scope = WriteScope(
                writer_id=writer_id,
                logical_operation_id=logical_operation_id,
                maintenance_epoch=maintenance_epoch,
                maintenance=maintenance,
                generation=self._generation,
            )
            self._active[scope_id] = scope
        token: Token[WriteScope | None] = _CURRENT_SCOPE.set(scope)
        try:
            yield scope
        finally:
            _CURRENT_SCOPE.reset(token)
            async with self._condition:
                self._active.pop(scope_id, None)
                self._condition.notify_all()

    def require_write_scope(self, *, maintenance_allowed: bool = True) -> WriteScope:
        scope = self.current_scope
        if scope is None:
            raise MaintenanceBlockedError("Agent Home mutation 缺少 WriteGate scope")
        if scope.maintenance and not maintenance_allowed:
            raise MaintenanceBlockedError("该写入不能在维护 scope 中执行")
        if scope.maintenance:
            if self._state is not MaintenanceState.FROZEN or scope.maintenance_epoch != self._epoch:
                raise MaintenanceBlockedError("维护 scope 已失效")
        elif scope.generation != self._generation:
            raise MaintenanceBlockedError("旧写入 scope 已到达 durable boundary，不得继续新操作")
        return scope

    def check_mutation_admission(self) -> None:
        """Low-level guard used by legacy Stores without changing every signature."""
        if self._state is MaintenanceState.RUNNING:
            return
        scope = self.current_scope
        if scope is None:
            raise MaintenanceBlockedError(
                f"Agent Home 当前为 {self._state.value}，mutation没有有效WriteScope",
            )
        if self._state is MaintenanceState.DRAINING:
            if scope.maintenance or scope.generation != self._generation:
                raise MaintenanceBlockedError("DRAINING只允许已有普通Scope完成当前边界")
            return
        if self._state is MaintenanceState.FROZEN:
            if not scope.maintenance or scope.maintenance_epoch != self._epoch:
                raise MaintenanceBlockedError("FROZEN只允许当前epoch维护写入")
            return
        raise MaintenanceBlockedError(f"Agent Home当前为{self._state.value}，写入已阻止")

    async def begin_draining(self, epoch: int) -> None:
        async with self._condition:
            if self._state is not MaintenanceState.RUNNING:
                raise MaintenanceBlockedError(f"无法从 {self._state.value} 开始维护")
            if epoch <= self._epoch:
                raise MaintenanceBlockedError("maintenance_epoch 必须严格递增")
            self._epoch = epoch
            self._state = MaintenanceState.DRAINING

    async def wait_for_idle(self, timeout_seconds: float) -> None:
        async with self._condition:
            await asyncio.wait_for(
                self._condition.wait_for(lambda: not self._active),
                timeout=timeout_seconds,
            )

    async def freeze(self, epoch: int) -> None:
        async with self._condition:
            if epoch != self._epoch or self._state is not MaintenanceState.DRAINING:
                raise MaintenanceBlockedError("只能冻结当前 DRAINING epoch")
            if self._active:
                raise MaintenanceBlockedError("仍存在未完成 Agent Home 写入")
            self._generation += 1
            self._state = MaintenanceState.FROZEN

    async def begin_resuming(self, epoch: int) -> None:
        async with self._condition:
            if epoch != self._epoch or self._state not in {
                MaintenanceState.FROZEN, MaintenanceState.FAILED,
            }:
                raise MaintenanceBlockedError("维护 epoch 或状态不匹配")
            self._state = MaintenanceState.RESUMING

    async def running(self, epoch: int) -> None:
        async with self._condition:
            if epoch != self._epoch or self._state is not MaintenanceState.RESUMING:
                raise MaintenanceBlockedError("只能完成当前 RESUMING epoch")
            self._generation += 1
            self._state = MaintenanceState.RUNNING
            self._condition.notify_all()

    async def fail(self, epoch: int) -> None:
        async with self._condition:
            if epoch == self._epoch:
                self._state = MaintenanceState.FAILED
                self._condition.notify_all()


class AgentHomeMaintenanceCoordinator:
    """Quiesces only active components and owns stable per-epoch exports."""

    def __init__(self, agent_root: Path, gate: AgentHomeWriteGate) -> None:
        self.agent_root = agent_root.resolve()
        self.gate = gate
        self.control_root = self.agent_root / ".yy-backups"
        self._participants: dict[str, MaintenanceParticipant] = {}
        self._lock = asyncio.Lock()
        self._external_lock: ExternalControlLock | None = None
        self._snapshot = MaintenanceSnapshot(
            state=MaintenanceState.RUNNING,
            maintenance_epoch=0,
        )

    def register(self, name: str, participant: MaintenanceParticipant) -> None:
        if not name or name in self._participants:
            raise ValueError(f"Maintenance Participant 名称重复或为空：{name!r}")
        self._participants[name] = participant

    @property
    def snapshot(self) -> MaintenanceSnapshot:
        return self._snapshot

    def participant_directory(self, epoch: int, name: str) -> Path:
        if epoch != self.gate.maintenance_epoch:
            raise MaintenanceBlockedError("不是当前 maintenance epoch")
        return self.control_root / "maintenance" / str(epoch) / "participants" / name

    async def freeze(self, reason: str, timeout_seconds: float = 300) -> MaintenanceSnapshot:
        async with self._lock:
            epoch = self._snapshot.maintenance_epoch + 1
            started = datetime.now().astimezone()
            external_lock = ExternalControlLock(self.control_root / "locks" / "maintenance.lock")
            external_lock.acquire()
            self._external_lock = external_lock
            try:
                await self.gate.begin_draining(epoch)
            except Exception:
                external_lock.close()
                self._external_lock = None
                raise
            self._snapshot = MaintenanceSnapshot(
                state=MaintenanceState.DRAINING,
                maintenance_epoch=epoch,
                reason=reason,
                started_at=started,
            )
            statuses: dict[str, QuiesceResult] = {}
            try:
                async def quiesce_all() -> None:
                    nonlocal statuses
                    results = await asyncio.gather(*(
                        participant.quiesce(epoch)
                        for participant in self._participants.values()
                    ))
                    statuses = {item.participant: item for item in results}
                    rejected = [item for item in results if not item.acknowledged or item.stale]
                    if rejected:
                        raise RuntimeError("Maintenance Participant 未全部 ACK")
                    await self.gate.wait_for_idle(timeout_seconds)
                    await self.gate.freeze(epoch)
                await asyncio.wait_for(quiesce_all(), timeout=timeout_seconds)
            except Exception as exc:
                await self.gate.fail(epoch)
                self._snapshot = MaintenanceSnapshot(
                    state=MaintenanceState.FAILED,
                    maintenance_epoch=epoch,
                    reason=reason,
                    started_at=started,
                    participant_status=statuses,
                    failure_reason=str(exc) or type(exc).__name__,
                )
                await self._resume_participants(epoch)
                await self.gate.begin_resuming(epoch)
                await self.gate.running(epoch)
                self._snapshot = self._snapshot.model_copy(update={"state": MaintenanceState.RUNNING})
                external_lock.close()
                self._external_lock = None
                raise
            self._snapshot = MaintenanceSnapshot(
                state=MaintenanceState.FROZEN,
                maintenance_epoch=epoch,
                reason=reason,
                started_at=started,
                participant_status=statuses,
            )
            return self._snapshot

    async def resume(self, epoch: int) -> None:
        async with self._lock:
            if epoch != self._snapshot.maintenance_epoch:
                return
            await self.gate.begin_resuming(epoch)
            self._snapshot = self._snapshot.model_copy(update={"state": MaintenanceState.RESUMING})
            await self._resume_participants(epoch)
            await self.gate.running(epoch)
            self._snapshot = self._snapshot.model_copy(update={"state": MaintenanceState.RUNNING})
            if self._external_lock is not None:
                self._external_lock.close()
                self._external_lock = None

    async def _resume_participants(self, epoch: int) -> None:
        await asyncio.gather(*(
            participant.resume(epoch)
            for participant in self._participants.values()
        ), return_exceptions=True)


__all__ = [
    "AgentHomeMaintenanceCoordinator",
    "AgentHomeWriteGate",
    "MaintenanceBlockedError",
    "MaintenanceParticipant",
    "WriteScope",
]
