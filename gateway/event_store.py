"""Canonical Gateway Event Store, cursor, projection rebuild and verified archive.

SQLite rows are the hot durable truth.  Files produced here are either a verified
archive containing the exact canonical bytes or a rebuildable projection; neither
is allowed to create business facts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from croniter import croniter

from gateway.models import GatewayEventEnvelope, now_iso


LOGGER = logging.getLogger(__name__)


def canonical_json(value: Any) -> str:
    """Return the repository-wide canonical JSON representation."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_canonical_event(raw: str) -> GatewayEventEnvelope:
    """Strictly parse v1/v2 bytes without rewriting the stored representation."""
    return GatewayEventEnvelope.model_validate_json(raw, strict=True)


class GatewayEventSchemaRegistry:
    """Small contract/upcaster registry; historical bytes are never rewritten."""

    def __init__(self) -> None:
        self._versions: dict[str, int] = {}

    def register(self, event_type: str, current_version: int) -> None:
        if current_version < 1:
            raise ValueError("Event schema version must be positive")
        existing = self._versions.get(event_type)
        if existing is not None and existing != current_version:
            raise ValueError(f"Event schema already registered: {event_type}")
        self._versions[event_type] = current_version

    def current_version(self, event_type: str) -> int:
        return self._versions.get(event_type, 1)

    @staticmethod
    def upcast_envelope(event: GatewayEventEnvelope) -> GatewayEventEnvelope:
        if event.version == 2:
            return event
        return event.model_copy(update={
            "stream_id": event.run_id,
            "stream_sequence": event.sequence,
            "event_type": event.type,
            "schema_version": 1,
        })


EVENT_SCHEMAS = GatewayEventSchemaRegistry()


def project_event(raw: str) -> GatewayEventEnvelope:
    """Upcast a historical envelope in memory while preserving its canonical bytes."""
    return EVENT_SCHEMAS.upcast_envelope(parse_canonical_event(raw))


@dataclass(frozen=True)
class CanonicalEventRecord:
    event_id: str
    stream_id: str
    stream_sequence: int
    canonical_event_json: str
    canonical_hash: str

    @property
    def envelope(self) -> GatewayEventEnvelope:
        return project_event(self.canonical_event_json)


class EventStoreIntegrityError(RuntimeError):
    """Canonical row, archive evidence or hash is inconsistent."""


class ProjectionConflict(RuntimeError):
    """A projection already contains the same identity with different bytes."""


class EventStore:
    """Read a unified stream across hot SQLite bodies and verified archives."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def read_canonical(self, event_id: str) -> CanonicalEventRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_id,stream_id,stream_sequence,canonical_event_json,canonical_hash,"
                "storage_tier,archive_id FROM gateway_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            raw = row["canonical_event_json"]
            if raw is None:
                raw = self._read_archive_member(connection, row)
            return self._validate_row(row, str(raw))

    def read_stream(
        self, stream_id: str, *, after_sequence: int = 0, limit: int | None = None,
    ) -> list[CanonicalEventRecord]:
        sql = (
            "SELECT event_id,stream_id,stream_sequence,canonical_event_json,canonical_hash,"
            "storage_tier,archive_id FROM gateway_events WHERE stream_id=? AND stream_sequence>? "
            "ORDER BY stream_sequence"
        )
        parameters: list[Any] = [stream_id, after_sequence]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
            result: list[CanonicalEventRecord] = []
            previous = after_sequence
            for row in rows:
                raw = row["canonical_event_json"]
                if raw is None:
                    raw = self._read_archive_member(connection, row)
                record = self._validate_row(row, str(raw))
                if record.stream_sequence <= previous:
                    raise EventStoreIntegrityError("Event stream is not strictly ordered")
                previous = record.stream_sequence
                result.append(record)
            return result

    def read_projection_stream(
        self, stream_id: str, *, after_sequence: int = 0, limit: int | None = None,
    ) -> list[GatewayEventEnvelope]:
        return [item.envelope for item in self.read_stream(
            stream_id, after_sequence=after_sequence, limit=limit,
        )]

    def max_sequence(self, stream_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(stream_sequence),0) FROM gateway_events WHERE stream_id=?",
                (stream_id,),
            ).fetchone()
        return int(row[0])

    def _read_archive_member(self, connection: sqlite3.Connection, row: sqlite3.Row) -> str:
        archive_id = row["archive_id"]
        if not archive_id:
            raise EventStoreIntegrityError("Archived event has no archive identity")
        member = connection.execute(
            "SELECT m.byte_offset,m.byte_length,a.archive_path,a.status,"
            "a.events_content_hash,a.manifest_hash,a.members_hash "
            "FROM gateway_event_archive_members m JOIN gateway_event_archives a USING(archive_id) "
            "WHERE m.archive_id=? AND m.event_id=?",
            (archive_id, row["event_id"]),
        ).fetchone()
        if member is None or member["status"] != "verified":
            raise EventStoreIntegrityError("Archived event has no verified member evidence")
        if member["byte_offset"] is None or member["byte_length"] is None:
            raise EventStoreIntegrityError("Verified archive member has no byte location")
        path = Path(str(member["archive_path"]))
        archive_bytes = path.read_bytes()
        newline = archive_bytes.find(b"\n")
        if newline < 0:
            raise EventStoreIntegrityError("Archive manifest is missing")
        try:
            header = json.loads(archive_bytes[:newline].decode("utf-8"))
        except Exception as exc:
            raise EventStoreIntegrityError("Archive manifest is invalid") from exc
        manifest = header.get("archive_manifest")
        if (
            not isinstance(manifest, dict)
            or header.get("manifest_hash") != member["manifest_hash"]
            or sha256_text(canonical_json(manifest)) != member["manifest_hash"]
            or manifest.get("members_hash") != member["members_hash"]
            or hashlib.sha256(archive_bytes[newline + 1:]).hexdigest()
            != member["events_content_hash"]
        ):
            raise EventStoreIntegrityError("Archive integrity evidence mismatch")
        offset = int(member["byte_offset"])
        length = int(member["byte_length"])
        data = archive_bytes[offset:offset + length]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EventStoreIntegrityError("Archive member is not UTF-8") from exc

    @staticmethod
    def _validate_row(row: sqlite3.Row, raw: str) -> CanonicalEventRecord:
        digest = sha256_text(raw)
        if digest != row["canonical_hash"]:
            raise EventStoreIntegrityError(f"Canonical hash mismatch for {row['event_id']}")
        event = parse_canonical_event(raw)
        stream_id = event.stream_id or event.run_id
        sequence = event.stream_sequence or event.sequence
        if (
            event.event_id != row["event_id"]
            or stream_id != row["stream_id"]
            or sequence != int(row["stream_sequence"])
        ):
            raise EventStoreIntegrityError("Canonical envelope identity differs from its index row")
        return CanonicalEventRecord(
            event_id=str(row["event_id"]),
            stream_id=str(row["stream_id"]),
            stream_sequence=int(row["stream_sequence"]),
            canonical_event_json=raw,
            canonical_hash=digest,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


class EventConsumerCursorStore:
    """CAS-only durable cursor; projection writes share the caller transaction."""

    @staticmethod
    def advance_in_transaction(
        connection: sqlite3.Connection,
        *,
        consumer_id: str,
        stream_id: str,
        expected_revision: int,
        processed_sequence: int,
    ) -> int:
        current = connection.execute(
            "SELECT last_sequence,revision FROM event_consumer_cursors "
            "WHERE consumer_id=? AND stream_id=?",
            (consumer_id, stream_id),
        ).fetchone()
        timestamp = now_iso()
        if current is None:
            if expected_revision != 0:
                raise EventStoreIntegrityError("Consumer cursor revision conflict")
            connection.execute(
                "INSERT INTO event_consumer_cursors VALUES(?,?,?,?,?)",
                (consumer_id, stream_id, processed_sequence, 1, timestamp),
            )
            return 1
        if int(current["revision"]) != expected_revision:
            raise EventStoreIntegrityError("Consumer cursor revision conflict")
        if processed_sequence < int(current["last_sequence"]):
            raise EventStoreIntegrityError("Consumer cursor cannot move backwards")
        cursor = connection.execute(
            "UPDATE event_consumer_cursors SET last_sequence=?,revision=revision+1,updated_at=? "
            "WHERE consumer_id=? AND stream_id=? AND revision=?",
            (processed_sequence, timestamp, consumer_id, stream_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise EventStoreIntegrityError("Consumer cursor CAS failed")
        return expected_revision + 1


class ProjectionRebuilder:
    """Rebuild JSONL solely from canonical EventStore records."""

    def __init__(self, store: EventStore, runs_directory: Path) -> None:
        self.store = store
        self.runs_directory = runs_directory.resolve()

    def rebuild_jsonl(self, stream_id: str) -> Path:
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        target = self.runs_directory / f"{stream_id}.jsonl"
        temporary = target.with_suffix(".jsonl.rebuild")
        records = self.store.read_stream(stream_id)
        with temporary.open("wb") as handle:
            for record in records:
                handle.write(record.canonical_event_json.encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if not _fsync_directory(target.parent):
            LOGGER.warning(
                "projection_directory_fsync_unavailable path=%s durability=file_fsync_only",
                target.parent,
            )
        return target


class GatewayEventArchiveService:
    """Freeze exact members, write bytes, verify, then release hot bodies."""

    FORMAT_VERSION = 1

    def __init__(self, database_path: Path, archive_directory: Path) -> None:
        self.database_path = database_path.resolve()
        self.archive_directory = archive_directory.resolve()

    def process_eligible(
        self,
        *,
        now: datetime | None = None,
        retention_days: int = 180,
        segment_max_events: int = 10_000,
    ) -> tuple[str, ...]:
        """Archive contiguous eligible hot prefixes, one verified segment per stream."""
        selected_now = now or datetime.now().astimezone()
        cutoff = (selected_now - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._connect() as connection:
            streams = connection.execute(
                "SELECT DISTINCT e.stream_id FROM gateway_events e JOIN runs r ON r.run_id=e.run_id "
                "JOIN agent_states a ON a.run_id=e.run_id "
                "WHERE e.storage_tier='hot' AND e.archive_id IS NULL AND e.occurred_at<? "
                "AND r.status IN ('completed','failed','cancelled','interrupted') "
                "AND a.recovery_required=0 ORDER BY e.stream_id",
                (cutoff,),
            ).fetchall()
        completed: list[str] = []
        for stream in streams:
            stream_id = str(stream["stream_id"])
            archive_id = self._prepare_eligible_stream(
                stream_id, cutoff=cutoff, segment_max_events=segment_max_events,
            )
            if archive_id is None:
                continue
            self.write_and_verify(archive_id)
            completed.append(archive_id)
        return tuple(completed)

    def _prepare_eligible_stream(
        self, stream_id: str, *, cutoff: str, segment_max_events: int,
    ) -> str | None:
        """Select and freeze the exact eligible member set in one transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT e.event_id,e.stream_id,e.stream_sequence,e.canonical_hash,e.storage_tier "
                "FROM gateway_events e JOIN runs r ON r.run_id=e.run_id "
                "JOIN agent_states s ON s.run_id=e.run_id "
                "WHERE e.stream_id=? AND e.storage_tier='hot' AND e.archive_id IS NULL "
                "AND e.occurred_at<? AND r.status IN ('completed','failed','cancelled','interrupted') "
                "AND s.recovery_required=0 "
                "AND NOT EXISTS (SELECT 1 FROM event_deliveries d WHERE d.event_id=e.event_id "
                "AND d.required=1 AND d.status!='delivered') "
                "AND NOT EXISTS (SELECT 1 FROM event_delivery_attempts a "
                "JOIN event_deliveries d ON d.delivery_id=a.delivery_id "
                "WHERE d.event_id=e.event_id AND a.status='delivering') "
                "ORDER BY e.stream_sequence LIMIT ?",
                (stream_id, cutoff, segment_max_events),
            ).fetchall()
            if not rows:
                connection.rollback()
                return None
            sequences = [int(row["stream_sequence"]) for row in rows]
            oldest_hot = connection.execute(
                "SELECT MIN(stream_sequence) FROM gateway_events WHERE stream_id=? "
                "AND storage_tier='hot' AND archive_id IS NULL",
                (stream_id,),
            ).fetchone()[0]
            if oldest_hot is None or sequences[0] != int(oldest_hot):
                connection.rollback()
                return None
            prefix_length = 1
            while (
                prefix_length < len(sequences)
                and sequences[prefix_length] == sequences[prefix_length - 1] + 1
            ):
                prefix_length += 1
            archive_id = self._insert_preparing(connection, stream_id, rows[:prefix_length])
            connection.commit()
            return archive_id

    def prepare(self, stream_id: str, event_ids: Iterable[str]) -> str:
        selected_ids = tuple(event_ids)
        if not selected_ids:
            raise ValueError("Archive requires at least one event")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in selected_ids)
            rows = connection.execute(
                f"SELECT event_id,stream_id,stream_sequence,canonical_hash,storage_tier "
                f"FROM gateway_events WHERE event_id IN ({placeholders}) ORDER BY stream_sequence",
                selected_ids,
            ).fetchall()
            if len(rows) != len(selected_ids):
                raise EventStoreIntegrityError("Archive selection contains missing events")
            sequences = [int(row["stream_sequence"]) for row in rows]
            if any(row["stream_id"] != stream_id or row["storage_tier"] != "hot" for row in rows):
                raise EventStoreIntegrityError("Archive members must be hot events from one stream")
            if sequences != list(range(sequences[0], sequences[-1] + 1)):
                raise EventStoreIntegrityError("Archive members must form a contiguous stream range")
            archive_id = self._insert_preparing(connection, stream_id, rows)
            connection.commit()
        return archive_id

    def _insert_preparing(
        self, connection: sqlite3.Connection, stream_id: str, rows: list[sqlite3.Row],
    ) -> str:
        archive_id = uuid4().hex
        timestamp = now_iso()
        sequences = [int(row["stream_sequence"]) for row in rows]
        stream_directory = hashlib.sha256(stream_id.encode("utf-8")).hexdigest()
        path = self.archive_directory / stream_directory / f"{archive_id}.events.jsonl"
        connection.execute(
            "INSERT INTO gateway_event_archives(archive_id,stream_id,first_sequence,last_sequence,"
            "event_count,events_content_hash,manifest_hash,members_hash,archive_path,status,"
            "created_at,verified_at,revision) VALUES(?,?,?,?,?,NULL,NULL,NULL,?,'preparing',?,NULL,0)",
            (archive_id, stream_id, sequences[0], sequences[-1], len(rows), str(path), timestamp),
        )
        for row in rows:
            connection.execute(
                "INSERT INTO gateway_event_archive_members(archive_id,event_id,stream_sequence,"
                "canonical_hash,byte_offset,byte_length) VALUES(?,?,?,?,NULL,NULL)",
                (archive_id, row["event_id"], row["stream_sequence"], row["canonical_hash"]),
            )
            changed = connection.execute(
                "UPDATE gateway_events SET archive_id=?,revision=revision+1 "
                "WHERE event_id=? AND storage_tier='hot' AND archive_id IS NULL",
                (archive_id, row["event_id"]),
            )
            if changed.rowcount != 1:
                raise EventStoreIntegrityError("Archive member reservation CAS failed")
        return archive_id

    def write_and_verify(self, archive_id: str) -> Path:
        with self._connect() as connection:
            archive = connection.execute(
                "SELECT * FROM gateway_event_archives WHERE archive_id=?", (archive_id,),
            ).fetchone()
            if archive is None:
                raise KeyError(archive_id)
            if archive["status"] == "verified":
                return Path(str(archive["archive_path"]))
            if archive["status"] != "preparing":
                raise EventStoreIntegrityError("Archive is not writable")
            # Recovery and normal writing use only this durable frozen member set.
            members = connection.execute(
                "SELECT m.*,e.canonical_event_json FROM gateway_event_archive_members m "
                "JOIN gateway_events e ON e.event_id=m.event_id "
                "WHERE m.archive_id=? ORDER BY m.stream_sequence",
                (archive_id,),
            ).fetchall()
        if len(members) != int(archive["event_count"]):
            raise EventStoreIntegrityError("Frozen archive member count changed")
        if any(member["canonical_event_json"] is None for member in members):
            raise EventStoreIntegrityError("PREPARING member lost its hot canonical body")

        events_bytes = b"".join(
            str(member["canonical_event_json"]).encode("utf-8") + b"\n" for member in members
        )
        events_content_hash = hashlib.sha256(events_bytes).hexdigest()
        members_payload = [{
            "event_id": member["event_id"],
            "stream_sequence": int(member["stream_sequence"]),
            "canonical_hash": member["canonical_hash"],
        } for member in members]
        members_hash = sha256_text(canonical_json(members_payload))
        manifest_payload = {
            "archive_id": archive_id,
            "format_version": self.FORMAT_VERSION,
            "stream_id": archive["stream_id"],
            "first_sequence": int(archive["first_sequence"]),
            "last_sequence": int(archive["last_sequence"]),
            "event_count": int(archive["event_count"]),
            "members_hash": members_hash,
            "events_content_hash": events_content_hash,
            "created_at": archive["created_at"],
        }
        # manifest_hash intentionally excludes itself and therefore cannot recurse.
        manifest_hash = sha256_text(canonical_json(manifest_payload))
        header = canonical_json({
            "archive_manifest": manifest_payload,
            "manifest_hash": manifest_hash,
        }).encode("utf-8") + b"\n"
        path = Path(str(archive["archive_path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        offsets: list[tuple[str, int, int]] = []
        offset = len(header)
        for member in members:
            raw = str(member["canonical_event_json"]).encode("utf-8")
            offsets.append((str(member["event_id"]), offset, len(raw)))
            offset += len(raw) + 1
        if not path.exists():
            with partial.open("wb") as handle:
                handle.write(header)
                for member in members:
                    raw = str(member["canonical_event_json"]).encode("utf-8")
                    handle.write(raw + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, path)
            if not _fsync_directory(path.parent):
                LOGGER.warning(
                    "archive_directory_fsync_unavailable path=%s durability=file_fsync_only",
                    path.parent,
                )
        # A formal file left by a crashed writer is evidence.  Verify it as-is;
        # never overwrite a conflicting file during recovery.
        self._verify_file(path, manifest_payload, manifest_hash, members, offsets)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status,revision FROM gateway_event_archives WHERE archive_id=?", (archive_id,),
            ).fetchone()
            if current is None or current["status"] != "preparing":
                raise EventStoreIntegrityError("Archive state changed during file write")
            for event_id, member_offset, member_length in offsets:
                connection.execute(
                    "UPDATE gateway_event_archive_members SET byte_offset=?,byte_length=? "
                    "WHERE archive_id=? AND event_id=?",
                    (member_offset, member_length, archive_id, event_id),
                )
            connection.execute(
                "UPDATE gateway_event_archives SET events_content_hash=?,manifest_hash=?,members_hash=?,"
                "status='verified',verified_at=?,revision=revision+1 WHERE archive_id=? AND revision=?",
                (events_content_hash, manifest_hash, members_hash, now_iso(), archive_id, current["revision"]),
            )
            connection.execute(
                "UPDATE gateway_events SET storage_tier='archived',canonical_event_json=NULL,"
                "revision=revision+1 WHERE archive_id=? AND storage_tier='hot'",
                (archive_id,),
            )
            connection.commit()
        return path

    @staticmethod
    def _verify_file(
        path: Path,
        manifest_payload: dict[str, Any],
        manifest_hash: str,
        members: list[sqlite3.Row],
        offsets: list[tuple[str, int, int]],
    ) -> None:
        data = path.read_bytes()
        first_newline = data.find(b"\n")
        if first_newline < 0:
            raise EventStoreIntegrityError("Archive manifest line is missing")
        header = json.loads(data[:first_newline].decode("utf-8"))
        if header != {"archive_manifest": manifest_payload, "manifest_hash": manifest_hash}:
            raise EventStoreIntegrityError("Archive manifest differs after fsync")
        if sha256_text(canonical_json(header["archive_manifest"])) != manifest_hash:
            raise EventStoreIntegrityError("Archive manifest hash mismatch")
        event_region = data[first_newline + 1:]
        if hashlib.sha256(event_region).hexdigest() != manifest_payload["events_content_hash"]:
            raise EventStoreIntegrityError("Archive events content hash mismatch")
        by_id = {str(item["event_id"]): item for item in members}
        for event_id, offset, length in offsets:
            raw = data[offset:offset + length]
            if hashlib.sha256(raw).hexdigest() != by_id[event_id]["canonical_hash"]:
                raise EventStoreIntegrityError(f"Archive member hash mismatch: {event_id}")

    def recover_preparing(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT archive_id,archive_path FROM gateway_event_archives WHERE status='preparing'",
            ).fetchall()
        results: dict[str, str] = {}
        for row in rows:
            archive_id = str(row["archive_id"])
            path = Path(str(row["archive_path"]))
            if path.exists():
                try:
                    self.write_and_verify(archive_id)
                except Exception:
                    self._set_archive_status(archive_id, "recovery_required")
                    results[archive_id] = "recovery_required"
                else:
                    results[archive_id] = "verified"
            else:
                self._set_archive_status(archive_id, "failed")
                results[archive_id] = "failed"
        return results

    def _set_archive_status(self, archive_id: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE gateway_event_archives SET status=?,revision=revision+1 WHERE archive_id=?",
                (status, archive_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


class GatewayEventArchiveScheduler:
    """Minimal local scheduler around the single verified archive implementation."""

    def __init__(
        self,
        service: GatewayEventArchiveService,
        *,
        schedule: str = "0 2 * * *",
        retention_days: int = 180,
        segment_max_events: int = 10_000,
    ) -> None:
        if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
            raise ValueError("Archive schedule must be a valid five-field cron")
        self.service = service
        self.schedule = schedule
        self.retention_days = retention_days
        self.segment_max_events = segment_max_events
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="gateway-event-archive")

    async def close(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            now = datetime.now().astimezone()
            due = croniter(self.schedule, now).get_next(datetime)
            delay = max(0.0, (due - now).total_seconds())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                await asyncio.to_thread(
                    self.service.process_eligible,
                    now=due,
                    retention_days=self.retention_days,
                    segment_max_events=self.segment_max_events,
                )


def _fsync_directory(directory: Path) -> bool:
    """Best effort: Windows commonly cannot open directories for fsync."""
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


__all__ = [
    "CanonicalEventRecord",
    "EventConsumerCursorStore",
    "EventStore",
    "EventStoreIntegrityError",
    "GatewayEventArchiveService",
    "GatewayEventArchiveScheduler",
    "GatewayEventSchemaRegistry",
    "ProjectionConflict",
    "ProjectionRebuilder",
    "canonical_json",
    "parse_canonical_event",
    "project_event",
    "sha256_text",
]
