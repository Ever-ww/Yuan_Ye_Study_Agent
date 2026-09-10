"""Single durable maintenance lifecycle and cooperative work admission."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path
import sqlite3
import threading
from typing import AsyncIterator, Protocol
from uuid import uuid4

from .models import MaintenanceSnapshot, MaintenanceState, QuiesceResult
from .control import ExternalControlLock, assert_restore_inactive
from .lifecycle_store import LifecycleStore


class MaintenanceBlockedError(RuntimeError):
    """New work is not admitted during maintenance."""


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
    gate_id: int
    scope_id: str
    kind: str


_CURRENT_SCOPE: ContextVar[WriteScope | None] = ContextVar("yy_agent_home_write_scope", default=None)
_CURRENT_GATE: ContextVar[AgentHomeWriteGate | None] = ContextVar("yy_current_write_gate", default=None)


class AgentHomeWriteGate:
    """Admission and transition share one thread lock; no await between check/register.

    Permits are transient liveness, NOT crash-replay instructions. Durable Run /
    Operation ledgers own recovery. Maintenance state has one external authority.
    """
    def __init__(self) -> None:
        self._store: LifecycleStore | None = None
        self._initial = MaintenanceSnapshot(state=MaintenanceState.RUNNING, maintenance_epoch=0)
        self._active: dict[str, WriteScope] = {}
        self._mutex = threading.RLock()
        self._storage_fault: Exception | None = None

    def bind_store(self, store: LifecycleStore) -> None:
        with self._mutex:
            if self._store is not None or self._active:
                raise MaintenanceBlockedError("Cannot rebind a live lifecycle")
            self._store = store

    @property
    def snapshot(self) -> MaintenanceSnapshot:
        with self._mutex:
            return self._store.read() if self._store else self._initial

    @property
    def state(self) -> MaintenanceState:
        return self.snapshot.state

    @property
    def maintenance_epoch(self) -> int:
        return self.snapshot.maintenance_epoch

    @property
    def current_scope(self) -> WriteScope | None:
        scope = _CURRENT_SCOPE.get()
        with self._mutex:
            return scope if scope and scope.gate_id == id(self) and scope.scope_id in self._active else None

    def transition(self, expected_state, target_state, reason, metadata=None, *, expected_revision=None):
        source, target = MaintenanceState(expected_state), MaintenanceState(target_state)
        allowed = {
            MaintenanceState.RUNNING: {MaintenanceState.QUIESCING, MaintenanceState.FAILED},
            MaintenanceState.QUIESCING: {MaintenanceState.QUIESCED, MaintenanceState.FAILED},
            MaintenanceState.QUIESCED: {MaintenanceState.RESTORING, MaintenanceState.RESUMING, MaintenanceState.FAILED},
            MaintenanceState.RESTORING: {MaintenanceState.RESUMING, MaintenanceState.FAILED},
            MaintenanceState.RESUMING: {MaintenanceState.RUNNING, MaintenanceState.FAILED},
            MaintenanceState.FAILED: {MaintenanceState.QUIESCING, MaintenanceState.RESUMING},
        }
        with self._mutex:
            old = self.snapshot
            if old.state != source or target not in allowed[source]:
                raise MaintenanceBlockedError(f"Illegal lifecycle transition {old.state.value} → {target.value}")
            if expected_revision is not None and old.revision != expected_revision:
                raise MaintenanceBlockedError("Stale lifecycle revision")
            if target in {MaintenanceState.QUIESCED, MaintenanceState.RESUMING, MaintenanceState.RUNNING} and self._active:
                raise MaintenanceBlockedError("Active work has not drained")
            fields = dict(metadata or {})
            if set(fields) - {"maintenance_epoch", "operation_id", "participant_status", "failure_reason", "started_at", "active_work"}:
                raise ValueError("Unsupported lifecycle metadata")
            now = datetime.now().astimezone()
            new = old.model_copy(update={**fields, "state": target, "reason": str(reason)[:500],
                                         "since": now, "revision": old.revision + 1})
            if self._store:
                try:
                    self._store.save(old, new)
                except (sqlite3.Error, OSError) as exc:
                    # A failed durable write cannot be represented as a successful
                    # transition. Fence this process; never serve from a stale cache.
                    self._storage_fault = exc
                    raise
            else:
                self._initial = new
            return new

    def get_lifecycle_state(self) -> MaintenanceSnapshot:
        with self._mutex:
            self._check_storage()
            values = tuple({"kind": s.kind, "writer_id": s.writer_id,
                            "operation_id": s.logical_operation_id} for s in self._active.values())
            counts = {kind: sum(s.kind == kind for s in self._active.values())
                      for kind in ("run", "tool", "background", "db")}
            return self.snapshot.model_copy(update={
                "active_runs": counts["run"], "active_tool_calls": counts["tool"],
                "active_background_jobs": counts["background"], "active_db_transactions": counts["db"],
                "active_work": values,
            })

    @contextmanager
    def work(self, writer_id, logical_operation_id, *, kind="background",
             maintenance_epoch=None, continuation=False):
        with self._mutex:
            self._check_storage()
            state = self.snapshot
            parent = self.current_scope
            maintenance = maintenance_epoch is not None
            if maintenance:
                if maintenance_epoch != state.maintenance_epoch or state.state not in {
                    MaintenanceState.QUIESCING, MaintenanceState.QUIESCED,
                    MaintenanceState.RESTORING, MaintenanceState.RESUMING,
                }:
                    raise MaintenanceBlockedError("Invalid maintenance scope")
            elif state.state != MaintenanceState.RUNNING:
                # Existing accepted work may finish, but cannot launch a new workflow.
                if not continuation or parent is None:
                    raise MaintenanceBlockedError(f"Gateway {state.state.value}: new work denied")
                if state.state not in {MaintenanceState.QUIESCING, MaintenanceState.FAILED}:
                    raise MaintenanceBlockedError("Agent Home is frozen")
            # The admitted generation remains valid when RUNNING → QUIESCING
            # increments the maintenance epoch. Existing scope identity is decisive.
            scope = WriteScope(str(writer_id), str(logical_operation_id), maintenance_epoch,
                               maintenance, state.maintenance_epoch, id(self), uuid4().hex, kind)
            self._active[scope.scope_id] = scope
        token = _CURRENT_SCOPE.set(scope)
        gate_token = _CURRENT_GATE.set(self)
        try:
            yield scope
        finally:
            _CURRENT_GATE.reset(gate_token)
            _CURRENT_SCOPE.reset(token)
            with self._mutex:
                self._active.pop(scope.scope_id, None)

    @asynccontextmanager
    async def operation(self, writer_id, logical_operation_id, *, maintenance_epoch=None,
                        kind=None, continuation=False) -> AsyncIterator[WriteScope]:
        selected = kind or ("run" if writer_id == "runtime_pool" else "background")
        with self.work(writer_id, logical_operation_id, kind=selected,
                       maintenance_epoch=maintenance_epoch, continuation=continuation) as scope:
            yield scope

    acquire_work = operation

    @contextmanager
    def existing_work_control(self, operation_id):
        """Only cancellation/decisions for existing Runs; never new dispatch.

        This keeps a waiting approval from making a drain impossible. Callers must
        resolve the target in the canonical Run/Approval ledger before entering.
        """
        with self._mutex:
            if self.state not in {MaintenanceState.RUNNING, MaintenanceState.QUIESCING, MaintenanceState.FAILED}:
                raise MaintenanceBlockedError("Frozen Gateway cannot mutate an existing Run")
            scope = WriteScope("existing-run-control", str(operation_id), None, False,
                               self.maintenance_epoch, id(self), uuid4().hex, "control")
            self._active[scope.scope_id] = scope
        token = _CURRENT_SCOPE.set(scope)
        try:
            yield
        finally:
            _CURRENT_SCOPE.reset(token)
            with self._mutex:
                self._active.pop(scope.scope_id, None)

    def require_write_scope(self, *, maintenance_allowed=True):
        scope = self.current_scope
        if scope is None or (scope.maintenance and not maintenance_allowed):
            raise MaintenanceBlockedError("Missing valid Agent Home WriteScope")
        self.check_mutation_admission()
        return scope

    def check_mutation_admission(self) -> None:
        with self._mutex:
            self._check_storage()
            state, scope = self.snapshot, self.current_scope
            if state.state == MaintenanceState.RUNNING:
                return
            if scope is not None:
                if scope.maintenance and scope.maintenance_epoch == state.maintenance_epoch:
                    return
                if not scope.maintenance and state.state in {MaintenanceState.QUIESCING, MaintenanceState.FAILED}:
                    return
            raise MaintenanceBlockedError(f"Gateway {state.state.value}: mutation denied")

    @contextmanager
    def database_transaction(self, *, read_only_allowed=False):
        # Synchronous state transactions finish before a transition can acquire
        # this same lock. This closes check/BEGIN races from executor threads.
        with self._mutex:
            if not read_only_allowed:
                self.check_mutation_admission()
            parent = self.current_scope
            scope_id = uuid4().hex
            self._active[scope_id] = WriteScope("sqlite", "transaction",
                parent.maintenance_epoch if parent else None, bool(parent and parent.maintenance),
                self.maintenance_epoch, id(self), scope_id, "db")
            try:
                yield
            finally:
                self._active.pop(scope_id, None)

    async def begin_draining(self, epoch: int) -> None:
        if epoch <= self.maintenance_epoch:
            raise MaintenanceBlockedError("maintenance_epoch must increase")
        with self._mutex:
            self.transition(self.state, MaintenanceState.QUIESCING, "quiesce",
                            {"maintenance_epoch": epoch, "operation_id": uuid4().hex,
                             "started_at": datetime.now().astimezone()})

    async def wait_for_idle(self, timeout_seconds: float) -> None:
        async def wait():
            while True:
                with self._mutex:
                    if not self._active:
                        return
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait(), timeout_seconds)

    async def freeze(self, epoch: int) -> None:
        self._check_epoch(epoch)
        self.transition(MaintenanceState.QUIESCING, MaintenanceState.QUIESCED, "work drained")

    async def begin_resuming(self, epoch: int) -> None:
        self._check_epoch(epoch)
        self.transition(self.state, MaintenanceState.RESUMING, "health validation")

    async def running(self, epoch: int) -> None:
        self._check_epoch(epoch)
        self.transition(MaintenanceState.RESUMING, MaintenanceState.RUNNING, "health checks passed",
                        {"failure_reason": None, "active_work": ()})

    async def fail(self, epoch: int, reason="maintenance failed") -> None:
        self._check_epoch(epoch)
        if self.state != MaintenanceState.FAILED:
            self.transition(self.state, MaintenanceState.FAILED, reason, {
                "failure_reason": str(reason)[:500], "active_work": self.get_lifecycle_state().active_work,
            })

    def _check_epoch(self, epoch):
        if epoch != self.maintenance_epoch:
            raise MaintenanceBlockedError("Stale maintenance epoch")

    def _check_storage(self):
        if self._storage_fault is not None:
            raise MaintenanceBlockedError("Lifecycle persistence failed; this process is fenced") from self._storage_fault


class AgentHomeMaintenanceCoordinator:
    """Single facade over the existing write gate, participants, and durable state."""
    def __init__(self, agent_root: Path, gate: AgentHomeWriteGate) -> None:
        self.agent_root = agent_root.resolve()
        self.gate = gate
        self.control_root = self.agent_root / ".yy-backups"
        self.store = LifecycleStore(self.agent_root)
        gate.bind_store(self.store)
        self._participants: dict[str, MaintenanceParticipant] = {}
        self._lock = asyncio.Lock()
        self._external_lock: ExternalControlLock | None = None
        self.health_check = None
        self.flush = None

    def register(self, name: str, participant: MaintenanceParticipant):
        if not name or name in self._participants:
            raise ValueError(f"Duplicate/empty Maintenance Participant: {name!r}")
        self._participants[name] = participant

    @property
    def snapshot(self):
        return self.gate.get_lifecycle_state()

    get_lifecycle_state = lambda self: self.snapshot

    def participant_directory(self, epoch, name):
        self.gate._check_epoch(epoch)
        if name not in self._participants:
            raise ValueError("Unknown participant")
        return self.control_root / "maintenance" / str(epoch) / "participants" / name

    async def quiesce(self, reason="maintenance", timeout=30):
        return await self.freeze(reason, timeout)

    async def freeze(self, reason: str, timeout_seconds: float = 300):
        if timeout_seconds <= 0:
            raise ValueError("Maintenance timeout must be positive")
        async with self._lock:
            if self.gate.current_scope is not None:
                raise MaintenanceBlockedError("Cannot drain the caller's own work lease")
            if self.gate.state not in {MaintenanceState.RUNNING, MaintenanceState.FAILED}:
                raise MaintenanceBlockedError("Maintenance already in progress")
            self._hold_external()
            epoch = self.snapshot.maintenance_epoch + 1
            statuses = {}
            try:
                self.gate.transition(self.gate.state, MaintenanceState.QUIESCING, reason, {
                    "maintenance_epoch": epoch, "operation_id": uuid4().hex,
                    "started_at": datetime.now().astimezone(), "failure_reason": None,
                })
                async def drain():
                    # First let already admitted workflows reach durable boundaries.
                    # New dispatches are blocked by the gate, not by boolean flags.
                    await self.gate.wait_for_idle(timeout_seconds)
                    async with self.gate.operation("maintenance-drain", str(epoch), maintenance_epoch=epoch):
                        # Trace-end callbacks may flush durable facts. They must
                        # finish before QUIESCED, not during later process exit.
                        for name, participant in self._participants.items():
                            result = await participant.quiesce(epoch)
                            statuses[name] = result
                            if not result.acknowledged or result.stale:
                                raise RuntimeError(f"Participant {name} did not acknowledge")
                        if self.flush:
                            await self.flush()
                    await self.gate.wait_for_idle(timeout_seconds)
                await asyncio.wait_for(drain(), timeout_seconds)
                self.gate.transition(MaintenanceState.QUIESCING, MaintenanceState.QUIESCED, reason,
                                     {"participant_status": statuses})
                return self.snapshot
            except BaseException as exc:
                await self.gate.fail(epoch, f"{type(exc).__name__}: {str(exc)[:300]}")
                # NO auto-resume on timeout/cancel; retain durable failure evidence.
                self._release_external()
                raise

    async def begin_restore(self, epoch: int):
        self.gate._check_epoch(epoch)
        return self.gate.transition(MaintenanceState.QUIESCED, MaintenanceState.RESTORING, "restore")

    async def resume(self, epoch: int, *, expected_revision=None):
        async with self._lock:
            self.gate._check_epoch(epoch)
            assert_restore_inactive(self.agent_root)
            state = self.snapshot
            if state.state not in {MaintenanceState.QUIESCED, MaintenanceState.RESTORING, MaintenanceState.FAILED}:
                raise MaintenanceBlockedError("Gateway is not awaiting resume")
            if expected_revision is not None and state.revision != expected_revision:
                raise MaintenanceBlockedError("Stale lifecycle revision")
            self._hold_external()
            try:
                self.gate.transition(self.gate.state, MaintenanceState.RESUMING, "resume requested",
                                     expected_revision=expected_revision)
                async with self.gate.operation("maintenance", str(epoch), maintenance_epoch=epoch):
                    # A failed drain may have stopped only some participants.
                    # Pause all again before touching/reconciling their stores.
                    for name, participant in self._participants.items():
                        ack = await participant.quiesce(epoch)
                        if not ack.acknowledged or ack.stale:
                            raise RuntimeError(f"Participant {name} is not at a safe boundary")
                    if self.health_check:
                        await self.health_check()
                    else:
                        validate_home_databases(self.agent_root)
                    # Errors are deliberately NOT swallowed.
                    for participant in self._participants.values():
                        await participant.resume(epoch)
                await self.gate.running(epoch)
            except BaseException as exc:
                await self.gate.fail(epoch, f"Resume {type(exc).__name__}: {str(exc)[:300]}")
                raise
            finally:
                self._release_external()

    async def fail(self, reason):
        await self.gate.fail(self.snapshot.maintenance_epoch, reason)
        self._release_external()

    def _hold_external(self):
        if self._external_lock is None:
            lock = ExternalControlLock(self.control_root / "locks/maintenance.lock")
            lock.acquire()
            self._external_lock = lock

    def _release_external(self):
        if self._external_lock:
            self._external_lock.close()
            self._external_lock = None

    def close(self):
        # Releasing a process lock NEVER means resuming service.
        self._release_external()


def validate_home_databases(agent_root: Path):
    """Integrity only; no schema migration or plaintext copying during checks."""
    root = agent_root / ".yy"
    for path in root.rglob("*.sqlite3"):
        if path.is_symlink():
            raise MaintenanceBlockedError("Database is a symlink")
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"SQLite integrity failed: {path.name}")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError(f"SQLite foreign keys failed: {path.name}")
        finally:
            db.close()


def lifecycle_work(kind="request", *, continuation=False):
    """Guard service entrypoints as well as HTTP callers, without a parallel FSM."""
    def decorate(function):
        @wraps(function)
        async def guarded(self, *args, **kwargs):
            gate = getattr(self, "write_gate", None)
            if gate is None:
                return await function(self, *args, **kwargs)
            async with gate.operation(function.__name__, uuid4().hex, kind=kind, continuation=continuation):
                return await function(self, *args, **kwargs)
        return guarded
    return decorate


@contextmanager
def child_work(writer_id: str, operation_id: str):
    """New child workflows inherit admission, NOT their parent's drain exception.

    Standalone runtimes have no Gateway gate. Gateway/Harness tasks carry it in
    their execution context, including child tasks and asyncio.to_thread calls.
    """
    gate = _CURRENT_GATE.get()
    if gate is None:
        yield
    else:
        with gate.work(writer_id, operation_id, kind="run"):
            yield


def lifecycle_mutation(function):
    """Synchronous metadata mutations share the same admission/transition lock."""
    @wraps(function)
    def guarded(self, *args, **kwargs):
        gate = getattr(self, "write_gate", None)
        if gate is None:
            return function(self, *args, **kwargs)
        with gate.database_transaction():
            return function(self, *args, **kwargs)
    return guarded
