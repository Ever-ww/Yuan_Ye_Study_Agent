"""Durable backup workflow state kept outside the replaceable ``.yy`` tree."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import BackupOperation, BackupOperationPhase


class BackupOperationConflict(RuntimeError):
    pass


_TERMINAL = {
    BackupOperationPhase.COMPLETED,
    BackupOperationPhase.FAILED,
    BackupOperationPhase.RECOVERY_REQUIRED,
}


class BackupOperationStore:
    """CAS store for the one active local backup workflow.

    This database is intentionally separate from both Gateway state and the
    backup payload.  Replacing ``.yy`` during restore cannot erase the evidence
    required to reconcile an interrupted backup.
    """

    def __init__(self, agent_root: Path) -> None:
        self.path = (
            agent_root.resolve()
            / ".yy-backups"
            / "control"
            / "backup"
            / "backup_operations.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupOperationConflict("Backup operation database failed quick_check")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS backup_operations (
                    operation_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    active_slot INTEGER,
                    revision INTEGER NOT NULL,
                    operation_json TEXT NOT NULL,
                    CHECK(active_slot IS NULL OR active_slot = 1)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_backup_operation
                    ON backup_operations(active_slot)
                    WHERE active_slot IS NOT NULL;
                CREATE TABLE IF NOT EXISTS backup_operation_transitions (
                    operation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    from_phase TEXT,
                    to_phase TEXT NOT NULL,
                    operation_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(operation_id, revision),
                    FOREIGN KEY(operation_id) REFERENCES backup_operations(operation_id)
                        ON DELETE RESTRICT
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def create(self, operation: BackupOperation) -> BackupOperation:
        if operation.revision != 0 or operation.phase in _TERMINAL:
            raise ValueError("A new backup operation must be active at revision zero")
        payload = operation.model_dump_json()
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO backup_operations VALUES(?,?,?,?,?)",
                    (operation.operation_id, operation.phase.value, 1, 0, payload),
                )
                db.execute(
                    "INSERT INTO backup_operation_transitions VALUES(?,?,?,?,?,?)",
                    (
                        operation.operation_id,
                        0,
                        None,
                        operation.phase.value,
                        payload,
                        operation.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise BackupOperationConflict("Another backup operation is already active") from exc
        return operation

    def get(self, operation_id: str) -> BackupOperation | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT operation_json,revision,phase FROM backup_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._decode(row) if row else None

    def active(self) -> BackupOperation | None:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT operation_json,revision,phase FROM backup_operations "
                "WHERE active_slot=1",
            ).fetchall()
        if len(rows) > 1:
            raise BackupOperationConflict("More than one active backup operation exists")
        return self._decode(rows[0]) if rows else None

    def transition(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        expected_phases: Iterable[BackupOperationPhase],
        phase: BackupOperationPhase,
        **updates: Any,
    ) -> BackupOperation:
        allowed = tuple(expected_phases)
        if not allowed:
            raise ValueError("expected_phases cannot be empty")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT operation_json,revision,phase FROM backup_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise BackupOperationConflict(f"Unknown backup operation: {operation_id}")
            current = self._decode(row)
            if current.revision != expected_revision or current.phase not in allowed:
                raise BackupOperationConflict(
                    f"Stale backup transition: revision={current.revision} phase={current.phase.value}",
                )
            now = datetime.now().astimezone()
            completed_at = now if phase in _TERMINAL else None
            next_value = current.model_copy(update={
                **updates,
                "phase": phase,
                "revision": current.revision + 1,
                "updated_at": now,
                "completed_at": completed_at,
            })
            payload = next_value.model_dump_json()
            active_slot = None if phase in _TERMINAL else 1
            changed = db.execute(
                "UPDATE backup_operations SET phase=?,active_slot=?,revision=?,operation_json=? "
                "WHERE operation_id=? AND revision=? AND phase=?",
                (
                    phase.value,
                    active_slot,
                    next_value.revision,
                    payload,
                    operation_id,
                    current.revision,
                    current.phase.value,
                ),
            ).rowcount
            if changed != 1:
                raise BackupOperationConflict("Backup operation CAS failed")
            db.execute(
                "INSERT INTO backup_operation_transitions VALUES(?,?,?,?,?,?)",
                (
                    operation_id,
                    next_value.revision,
                    current.phase.value,
                    phase.value,
                    payload,
                    now.isoformat(),
                ),
            )
            return next_value

    @staticmethod
    def _decode(row: tuple[str, int, str]) -> BackupOperation:
        operation = BackupOperation.model_validate_json(row[0])
        if operation.revision != row[1] or operation.phase.value != row[2]:
            raise BackupOperationConflict("Backup operation row/json invariant failed")
        return operation


__all__ = ["BackupOperationConflict", "BackupOperationStore"]
