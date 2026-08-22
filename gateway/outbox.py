"""Transactional Outbox delivery over frozen per-event sink snapshots.

Delivery attempts are physical evidence only.  Retry eligibility and aggregate
completion live on ``event_deliveries``/``event_outbox`` and never modify domain
state or manufacture a new Gateway Event.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from backup import QuiesceResult
from gateway.audit import AuditSanitizer
from gateway.event_store import ProjectionConflict, parse_canonical_event, sha256_text
from gateway.models import GatewayEventEnvelope, now_iso


DeliverEvent = Callable[[GatewayEventEnvelope], Awaitable[None]]
LOGGER = logging.getLogger(__name__)


class JsonlEventSink:
    """Append exact canonical bytes once, with an identity/content conflict guard."""

    def __init__(self, runs_directory: Path) -> None:
        self.runs_directory = runs_directory.resolve()
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self._known: dict[str, dict[str, str]] = {}

    def append_once(
        self,
        canonical_event_json: str,
        canonical_hash: str,
        *,
        run_id: str | None = None,
    ) -> bool:
        # The sink is deliberately string-only: accepting an Envelope here would make
        # accidental reserialization of canonical history possible.
        raw = canonical_event_json
        event = parse_canonical_event(raw)
        selected_run_id = run_id or event.run_id
        selected_hash = canonical_hash
        if sha256_text(raw) != selected_hash:
            raise ProjectionConflict("JSONL input hash does not match canonical content")
        path = self.runs_directory / f"{selected_run_id}.jsonl"
        known = self._known.get(selected_run_id)
        if known is None:
            known = self._load_index(path)
            self._known[selected_run_id] = known
            self._rewrite_index(selected_run_id, known)
        existing = known.get(event.event_id)
        if existing is not None:
            if existing != selected_hash:
                raise ProjectionConflict(
                    f"event_id {event.event_id} already has different canonical content",
                )
            return False
        created = not path.exists()
        with path.open("ab") as handle:
            handle.write(raw.encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            if not _fsync_directory(path.parent):
                LOGGER.warning(
                    "jsonl_directory_fsync_unavailable path=%s durability=file_fsync_only",
                    path.parent,
                )
        known[event.event_id] = selected_hash
        with self._index_path(selected_run_id).open("a", encoding="ascii", newline="\n") as handle:
            handle.write(f"{event.event_id} {selected_hash}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    @staticmethod
    def _load_index(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not path.exists():
            return values
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = parse_canonical_event(line.rstrip("\n"))
                except Exception as exc:
                    raise RuntimeError(f"Gateway Event JSONL contains a damaged record: {path}") from exc
                digest = sha256_text(line.rstrip("\n"))
                existing = values.get(event.event_id)
                if existing is not None and existing != digest:
                    raise ProjectionConflict(f"Duplicate JSONL event identity: {event.event_id}")
                values[event.event_id] = digest
        return values

    def _index_path(self, run_id: str) -> Path:
        return self.runs_directory / f"{run_id}.events.idx"

    def _rewrite_index(self, run_id: str, values: dict[str, str]) -> None:
        path = self._index_path(run_id)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="ascii", newline="\n") as handle:
            for event_id, digest in sorted(values.items()):
                handle.write(f"{event_id} {digest}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)


@dataclass(frozen=True)
class ClaimedDelivery:
    delivery_id: str
    attempt_id: str
    attempt_no: int
    revision: int
    sink_id: str
    event_id: str
    canonical_event_json: str
    canonical_hash: str


class OutboxDispatcher:
    """CAS-claim due deliveries and settle each sink independently."""

    def __init__(
        self,
        database_path: Path,
        runs_directory: Path,
        publish: DeliverEvent,
        *,
        gateway_epoch: str = "legacy",
        poll_seconds: float = 0.1,
        retry_max_attempts: int = 12,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 900.0,
        dead_letter_enabled: bool = True,
    ) -> None:
        self.database_path = database_path.resolve()
        self.jsonl = JsonlEventSink(runs_directory)
        self.deliver_event = publish
        self.gateway_epoch = gateway_epoch
        self.poll_seconds = poll_seconds
        self.retry_max_attempts = retry_max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.dead_letter_enabled = dead_letter_enabled
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._drain_lock = asyncio.Lock()
        self._paused_epoch: int | None = None

    async def start(self) -> None:
        self.reconcile_startup()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="gateway-outbox-dispatcher")

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult:
        if self._paused_epoch is not None and maintenance_epoch <= self._paused_epoch:
            return QuiesceResult(
                participant="outbox", maintenance_epoch=maintenance_epoch,
                acknowledged=maintenance_epoch == self._paused_epoch,
                stale=maintenance_epoch < self._paused_epoch,
                safe_boundary="delivery_attempt_persisted" if maintenance_epoch == self._paused_epoch else None,
            )
        self._paused_epoch = maintenance_epoch
        self._wake.set()
        async with self._drain_lock:
            pass
        return QuiesceResult(
            participant="outbox", maintenance_epoch=maintenance_epoch, acknowledged=True,
            safe_boundary="delivery_attempt_persisted_backlog_preserved",
        )

    async def resume(self, maintenance_epoch: int) -> None:
        if self._paused_epoch == maintenance_epoch:
            self._paused_epoch = None
            self._wake.set()

    async def drain_once(self) -> int:
        async with self._drain_lock:
            due = self._due_deliveries()
            claimed_count = 0
            for candidate in due:
                claim = self._claim(str(candidate["delivery_id"]), int(candidate["revision"]))
                if claim is None:
                    continue
                claimed_count += 1
                try:
                    if claim.sink_id == "eventbus":
                        await self.deliver_event(parse_canonical_event(claim.canonical_event_json))
                    elif claim.sink_id == "jsonl":
                        self.jsonl.append_once(
                            claim.canonical_event_json, claim.canonical_hash,
                        )
                    else:
                        raise RuntimeError(f"No registered Event sink: {claim.sink_id}")
                except asyncio.CancelledError:
                    # The active attempt remains durable and startup reconcile marks it
                    # interrupted.  Do not pretend a physical call reached a result.
                    raise
                except Exception as exc:
                    self._finish_failure(claim, exc)
                else:
                    self._finish_success(claim)
            return claimed_count

    async def _run(self) -> None:
        while True:
            if self._paused_epoch is not None:
                self._wake.clear()
                await self._wake.wait()
                continue
            processed = await self.drain_once()
            if processed:
                await asyncio.sleep(0)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def _due_deliveries(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT d.delivery_id,d.revision FROM event_deliveries d "
                "JOIN event_outbox o ON o.outbox_id=d.outbox_id "
                "WHERE d.status IN ('pending','retrying') "
                "AND (d.next_retry_at IS NULL OR d.next_retry_at<=?) "
                "ORDER BY o.created_at,d.event_id,d.sink_id LIMIT 100",
                (now_iso(),),
            ).fetchall()

    def _claim(self, delivery_id: str, expected_revision: int) -> ClaimedDelivery | None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE event_deliveries SET status='delivering',attempt_count=attempt_count+1,"
                "revision=revision+1 WHERE delivery_id=? AND revision=? "
                "AND status IN ('pending','retrying') "
                "AND (next_retry_at IS NULL OR next_retry_at<=?)",
                (delivery_id, expected_revision, timestamp),
            )
            if changed.rowcount != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT d.*,e.canonical_event_json,e.canonical_hash FROM event_deliveries d "
                "JOIN gateway_events e ON e.event_id=d.event_id WHERE d.delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is None or row["canonical_event_json"] is None:
                connection.rollback()
                raise RuntimeError("Due delivery has no hot canonical event body")
            attempt_no = int(row["attempt_count"])
            attempt_id = hashlib.sha256(
                f"event-delivery-attempt:{delivery_id}:{attempt_no}".encode("utf-8"),
            ).hexdigest()
            connection.execute(
                "INSERT INTO event_delivery_attempts(attempt_id,delivery_id,attempt_no,gateway_epoch,"
                "status,started_at,completed_at,error_type,error_summary) "
                "VALUES(?,?,?,?,'delivering',?,NULL,NULL,NULL)",
                (attempt_id, delivery_id, attempt_no, self.gateway_epoch, timestamp),
            )
            connection.commit()
            return ClaimedDelivery(
                delivery_id=delivery_id, attempt_id=attempt_id, attempt_no=attempt_no,
                revision=int(row["revision"]), sink_id=str(row["sink_id"]),
                event_id=str(row["event_id"]),
                canonical_event_json=str(row["canonical_event_json"]),
                canonical_hash=str(row["canonical_hash"]),
            )

    def _finish_success(self, claim: ClaimedDelivery) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._settle_attempt(connection, claim, "succeeded", timestamp)
            changed = connection.execute(
                "UPDATE event_deliveries SET status='delivered',next_retry_at=NULL,"
                "last_error_type=NULL,last_error=NULL,delivered_at=?,revision=revision+1 "
                "WHERE delivery_id=? AND revision=? AND status='delivering'",
                (timestamp, claim.delivery_id, claim.revision),
            )
            if changed.rowcount != 1:
                connection.rollback()
                raise RuntimeError("Delivery completion CAS failed")
            self._complete_outbox_if_ready(connection, claim.event_id, timestamp)
            connection.commit()

    def _finish_failure(self, claim: ClaimedDelivery, error: Exception) -> None:
        timestamp = now_iso()
        error_type = type(error).__name__
        summary = str(AuditSanitizer.sanitize(str(error) or error_type))[:2000]
        dead = self.dead_letter_enabled and claim.attempt_no >= self.retry_max_attempts
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, claim.attempt_no - 1)),
        )
        next_retry = None if dead else (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._settle_attempt(
                connection, claim, "failed", timestamp,
                error_type=error_type, error_summary=summary,
            )
            status = "dead_lettered" if dead else "retrying"
            changed = connection.execute(
                "UPDATE event_deliveries SET status=?,next_retry_at=?,last_error_type=?,last_error=?,"
                "dead_lettered_at=?,revision=revision+1 WHERE delivery_id=? AND revision=? "
                "AND status='delivering'",
                (
                    status, next_retry, error_type, summary, timestamp if dead else None,
                    claim.delivery_id, claim.revision,
                ),
            )
            if changed.rowcount != 1:
                connection.rollback()
                raise RuntimeError("Delivery failure CAS failed")
            connection.commit()

    @staticmethod
    def _settle_attempt(
        connection: sqlite3.Connection,
        claim: ClaimedDelivery,
        status: str,
        timestamp: str,
        *,
        error_type: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        changed = connection.execute(
            "UPDATE event_delivery_attempts SET status=?,completed_at=?,error_type=?,error_summary=? "
            "WHERE attempt_id=? AND status='delivering'",
            (status, timestamp, error_type, error_summary, claim.attempt_id),
        )
        if changed.rowcount != 1:
            raise RuntimeError("Delivery attempt is no longer active")

    @staticmethod
    def _complete_outbox_if_ready(
        connection: sqlite3.Connection, event_id: str, timestamp: str,
    ) -> None:
        # Completion reads only frozen Delivery.required values.  event_sinks is
        # deliberately absent from this query.
        connection.execute(
            "UPDATE event_outbox SET completed_at=?,revision=revision+1 WHERE event_id=? "
            "AND completed_at IS NULL AND NOT EXISTS (SELECT 1 FROM event_deliveries d "
            "WHERE d.event_id=event_outbox.event_id AND d.required=1 AND d.status!='delivered')",
            (timestamp, event_id),
        )

    def reconcile_startup(self) -> dict[str, int]:
        """Interrupt orphan physical calls and restore delivery retry eligibility."""
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            orphan_count = int(connection.execute(
                "SELECT COUNT(*) FROM event_delivery_attempts a LEFT JOIN event_deliveries d "
                "ON d.delivery_id=a.delivery_id WHERE d.delivery_id IS NULL",
            ).fetchone()[0])
            event_orphans = int(connection.execute(
                "SELECT COUNT(*) FROM event_outbox o LEFT JOIN gateway_events e "
                "ON e.event_id=o.event_id WHERE e.event_id IS NULL",
            ).fetchone()[0])
            delivery_orphans = int(connection.execute(
                "SELECT COUNT(*) FROM event_deliveries d LEFT JOIN event_outbox o "
                "ON o.outbox_id=d.outbox_id LEFT JOIN gateway_events e ON e.event_id=d.event_id "
                "WHERE o.outbox_id IS NULL OR e.event_id IS NULL",
            ).fetchone()[0])
            if orphan_count or event_orphans or delivery_orphans:
                connection.rollback()
                raise RuntimeError("Gateway Event delivery invariant violation")
            active = connection.execute(
                "SELECT attempt_id,delivery_id FROM event_delivery_attempts WHERE status='delivering'",
            ).fetchall()
            for attempt in active:
                connection.execute(
                    "UPDATE event_delivery_attempts SET status='interrupted',completed_at=?,"
                    "error_type='GatewayRestart',error_summary='delivery interrupted before durable result' "
                    "WHERE attempt_id=? AND status='delivering'",
                    (timestamp, attempt["attempt_id"]),
                )
                connection.execute(
                    "UPDATE event_deliveries SET status='retrying',next_retry_at=?,"
                    "last_error_type='GatewayRestart',last_error='delivery interrupted',"
                    "revision=revision+1 WHERE delivery_id=? AND status='delivering'",
                    (timestamp, attempt["delivery_id"]),
                )
            completed = connection.execute(
                "UPDATE event_outbox SET completed_at=?,revision=revision+1 WHERE completed_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM event_deliveries d WHERE d.event_id=event_outbox.event_id "
                "AND d.required=1 AND d.status!='delivered')",
                (timestamp,),
            ).rowcount
            connection.commit()
        return {"interrupted": len(active), "completed_outbox": int(completed)}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


def _fsync_directory(directory: Path) -> bool:
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


__all__ = ["JsonlEventSink", "OutboxDispatcher", "ProjectionConflict"]
