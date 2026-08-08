"""Gateway Event 的 EventBus/JSONL 多 Sink Outbox。"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from gateway.audit import AuditSanitizer
from gateway.models import GatewayEventEnvelope, now_iso


PublishEvent = Callable[[GatewayEventEnvelope], Awaitable[None]]


class JsonlEventSink:
    """使用稳定 event_id 实现 append_once。"""

    def __init__(self, runs_directory: Path) -> None:
        self.runs_directory = runs_directory.resolve()
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self._known: dict[str, set[str]] = {}

    def append_once(self, event: GatewayEventEnvelope) -> bool:
        path = self.runs_directory / f"{event.run_id}.jsonl"
        known = self._known.get(event.run_id)
        if known is None:
            known = set()
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        known.add(GatewayEventEnvelope.model_validate_json(line, strict=True).event_id)
                    except Exception:
                        # 损坏行不能被悄悄覆盖；Outbox 会持续报告写入失败。
                        raise RuntimeError(f"Gateway Event JSONL 存在损坏记录：{path}")
            self._known[event.run_id] = known
            self._rewrite_index(event.run_id, known)
        if event.event_id in known:
            return False
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
        known.add(event.event_id)
        with self._index_path(event.run_id).open("a", encoding="ascii", newline="\n") as handle:
            handle.write(event.event_id + "\n")
            handle.flush()
        return True

    def _index_path(self, run_id: str) -> Path:
        return self.runs_directory / f"{run_id}.events.idx"

    def _rewrite_index(self, run_id: str, values: set[str]) -> None:
        path = self._index_path(run_id)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("".join(f"{item}\n" for item in sorted(values)), encoding="ascii")
        temporary.replace(path)


class OutboxDispatcher:
    """各 Sink 独立确认；两者都成功后才标记 delivered。"""

    def __init__(
        self,
        database_path: Path,
        runs_directory: Path,
        publish: PublishEvent,
        *,
        poll_seconds: float = 0.1,
        retry_max_attempts: int = 12,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 900.0,
        dead_letter_enabled: bool = True,
    ) -> None:
        self.database_path = database_path.resolve()
        self.jsonl = JsonlEventSink(runs_directory)
        self.publish = publish
        self.poll_seconds = poll_seconds
        self.retry_max_attempts = retry_max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.dead_letter_enabled = dead_letter_enabled
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._drain_lock = asyncio.Lock()

    async def start(self) -> None:
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

    async def drain_once(self) -> int:
        async with self._drain_lock:
            rows = self._pending()
            for row in rows:
                event = GatewayEventEnvelope.model_validate_json(row["event_json"], strict=True)
                if self._sink_due(row, "eventbus"):
                    try:
                        await self.publish(event)
                    except Exception as exc:
                        self._mark_failure(event.event_id, "eventbus", exc)
                    else:
                        self._mark_sent(event.event_id, "eventbus")
                refreshed = self._outbox(event.event_id)
                if self._sink_due(refreshed, "jsonl"):
                    try:
                        self.jsonl.append_once(event)
                    except Exception as exc:
                        self._mark_failure(event.event_id, "jsonl", exc)
                    else:
                        self._mark_sent(event.event_id, "jsonl")
                self._mark_delivered_if_complete(event.event_id)
            return len(rows)

    @staticmethod
    def _sink_due(row: sqlite3.Row, sink: str) -> bool:
        if row[f"{sink}_status"] == "sent" or row[f"{sink}_dead_letter_at"] is not None:
            return False
        value = row[f"{sink}_next_attempt_at"]
        if value is None:
            return True
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) <= datetime.now(timezone.utc)

    async def _run(self) -> None:
        while True:
            processed = await self.drain_once()
            if processed:
                await asyncio.sleep(0)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def _pending(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT o.*,e.event_json FROM event_outbox o JOIN gateway_events e USING(event_id) "
                "WHERE o.delivered_at IS NULL AND ("
                "(o.eventbus_status!='sent' AND o.eventbus_dead_letter_at IS NULL "
                " AND (o.eventbus_next_attempt_at IS NULL OR o.eventbus_next_attempt_at<=?)) OR "
                "(o.jsonl_status!='sent' AND o.jsonl_dead_letter_at IS NULL "
                " AND (o.jsonl_next_attempt_at IS NULL OR o.jsonl_next_attempt_at<=?))) "
                "ORDER BY o.created_at,o.sequence LIMIT 100",
                (now_iso(), now_iso()),
            ).fetchall()

    def _outbox(self, event_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM event_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return row

    def _mark_sent(self, event_id: str, sink: str) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE event_outbox SET {sink}_status='sent',{sink}_attempts={sink}_attempts+1,"
                f"{sink}_next_attempt_at=NULL,last_error=NULL,updated_at=? WHERE event_id=?",
                (timestamp, event_id),
            )
            connection.commit()

    def _mark_failure(self, event_id: str, sink: str, error: Exception) -> None:
        timestamp = now_iso()
        selected = str(AuditSanitizer.sanitize(str(error) or type(error).__name__))[:2000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {sink}_attempts FROM event_outbox WHERE event_id=?", (event_id,),
            ).fetchone()
            attempts = int(row[0]) + 1 if row else 1
            dead_letter_at = (
                timestamp if self.dead_letter_enabled and attempts >= self.retry_max_attempts else None
            )
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, attempts - 1)),
            )
            next_attempt_at = None if dead_letter_at else (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat()
            connection.execute(
                f"UPDATE event_outbox SET {sink}_status='failed',{sink}_attempts={sink}_attempts+1,"
                f"{sink}_next_attempt_at=?,{sink}_dead_letter_at=?,last_error=?,updated_at=? "
                "WHERE event_id=?",
                (next_attempt_at, dead_letter_at, selected, timestamp, event_id),
            )
            connection.commit()

    def _mark_delivered_if_complete(self, event_id: str) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE event_outbox SET delivered_at=?,updated_at=? WHERE event_id=? "
                "AND eventbus_status='sent' AND jsonl_status='sent' AND delivered_at IS NULL",
                (timestamp, timestamp, event_id),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


__all__ = ["JsonlEventSink", "OutboxDispatcher"]
