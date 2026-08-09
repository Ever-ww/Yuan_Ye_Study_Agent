"""Agent Home external restore fence, locks, and append-only journal."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import RestoreFence, RestoreJournalRecord


class RestoreFenceActiveError(RuntimeError):
    pass


class ExternalControlLock:
    """Cross-process lock stored outside the replaceable Agent Home."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        handle = self.path.open("r+b")
        if self.path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("Agent Home已有Backup/Restore维护任务") from exc
        self.handle = handle

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def external_control_root(agent_root: Path) -> Path:
    """Return a control path without creating it or touching Agent Home."""
    return agent_root.resolve() / ".yy-backups"


def restore_fence_path(agent_root: Path) -> Path:
    return external_control_root(agent_root) / "restores" / "active-fence.json"


def read_restore_fence(agent_root: Path) -> RestoreFence | None:
    path = restore_fence_path(agent_root)
    if not path.is_file():
        return None
    return RestoreFence.model_validate_json(path.read_text(encoding="utf-8"), strict=True)


def assert_restore_inactive(agent_root: Path) -> None:
    fence = read_restore_fence(agent_root)
    if fence is not None:
        raise RestoreFenceActiveError(
            f"Agent Home 正在恢复（restore_id={fence.restore_id}）；只允许 backup recover/rollback/status",
        )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.partial")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def create_restore_fence(agent_root: Path, fence: RestoreFence) -> Path:
    path = restore_fence_path(agent_root)
    if path.exists():
        current = read_restore_fence(agent_root)
        if current == fence:
            return path
        raise RestoreFenceActiveError("另一个 Restore 已经持有 Agent Home Fence")
    _atomic_json(path, fence.model_dump_json(indent=2))
    return path


def remove_restore_fence(agent_root: Path, restore_id: str) -> None:
    path = restore_fence_path(agent_root)
    current = read_restore_fence(agent_root)
    if current is None:
        return
    if current.restore_id != restore_id:
        raise RestoreFenceActiveError("不能移除其他 Restore 的 Fence")
    path.unlink()
    _fsync_directory(path.parent)


class RestoreJournal:
    """Append-only hash-chained restore journal.

    The journal itself is the state authority. The fence merely locates it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.Lock()

    def records(self) -> tuple[RestoreJournalRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[RestoreJournalRecord] = []
        previous = "0" * 64
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = RestoreJournalRecord.model_validate_json(line, strict=True)
            except Exception as exc:
                raise RuntimeError(f"Restore Journal 第 {number} 行损坏") from exc
            if record.sequence != len(records) + 1 or record.previous_record_hash != previous:
                raise RuntimeError("Restore Journal 序号或哈希链断裂")
            expected = self._hash_payload(
                record.sequence,
                record.previous_record_hash,
                record.record_type,
                record.action_id,
                record.payload,
                record.timestamp,
            )
            if record.record_hash != expected:
                raise RuntimeError("Restore Journal 记录哈希无效")
            records.append(record)
            previous = record.record_hash
        return tuple(records)

    def append(
        self,
        record_type: str,
        payload: dict[str, Any],
        *,
        action_id: str | None = None,
    ) -> RestoreJournalRecord:
        with self._lock:
            existing = self.records()
            sequence = len(existing) + 1
            previous = existing[-1].record_hash if existing else "0" * 64
            timestamp = datetime.now().astimezone()
            record_hash = self._hash_payload(
                sequence, previous, record_type, action_id, payload, timestamp,
            )
            record = RestoreJournalRecord(
                sequence=sequence,
                previous_record_hash=previous,
                record_type=record_type,
                action_id=action_id,
                payload=payload,
                record_hash=record_hash,
                timestamp=timestamp,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(record.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(self.path.parent)
            return record

    def begin_action(self, action_id: str, payload: dict[str, Any]) -> RestoreJournalRecord:
        return self.append("action_intent", payload, action_id=action_id)

    def commit_action(self, action_id: str, payload: dict[str, Any]) -> RestoreJournalRecord:
        intents = {
            item.action_id for item in self.records() if item.record_type == "action_intent"
        }
        if action_id not in intents:
            raise RuntimeError("action_committed 缺少已持久化的 intent")
        return self.append("action_committed", payload, action_id=action_id)

    @staticmethod
    def _hash_payload(
        sequence: int,
        previous: str,
        record_type: str,
        action_id: str | None,
        payload: dict[str, Any],
        timestamp: datetime,
    ) -> str:
        value = {
            "sequence": sequence,
            "previous_record_hash": previous,
            "record_type": record_type,
            "action_id": action_id,
            "payload": payload,
            "timestamp": timestamp.isoformat(),
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ExternalControlLock",
    "RestoreFenceActiveError",
    "RestoreJournal",
    "assert_restore_inactive",
    "create_restore_fence",
    "external_control_root",
    "read_restore_fence",
    "remove_restore_fence",
    "restore_fence_path",
]
