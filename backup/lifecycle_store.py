"""Maintenance control facts live OUTSIDE the replaceable .yy tree.

This is the single lifecycle authority, not a second Run/Event store. Transition
evidence and the singleton snapshot commit together; instance.json and stop files
are discovery/requests only. No connection remains open across a restore.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3

from .models import MaintenanceSnapshot, MaintenanceState


class LifecycleConflict(RuntimeError):
    pass


class LifecycleStore:
    def __init__(self, agent_root: Path):
        self.path = agent_root.resolve() / ".yy-backups/control/gateway/lifecycle.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LifecycleConflict("Lifecycle database failed quick_check")
            db.execute("BEGIN IMMEDIATE")
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'lifecycle_%'")}
            expected = {"lifecycle_state", "lifecycle_transitions"}
            if names and names != expected:
                raise LifecycleConflict("Incomplete lifecycle schema; recovery required")
            if names:
                if db.execute("SELECT COUNT(*) FROM lifecycle_state").fetchone()[0] != 1:
                    raise LifecycleConflict("Missing lifecycle snapshot; cannot assume RUNNING")
                return
            db.execute("""CREATE TABLE lifecycle_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE lifecycle_transitions (
                    revision INTEGER PRIMARY KEY, from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL, snapshot_json TEXT NOT NULL)""")
            initial = MaintenanceSnapshot(state=MaintenanceState.RUNNING, maintenance_epoch=0,
                                          since=datetime.now().astimezone())
            db.execute("INSERT INTO lifecycle_state VALUES(1,0,?)", (initial.model_dump_json(),))

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def read(self) -> MaintenanceSnapshot:
        with closing(self._connect()) as db:
            row = db.execute("SELECT snapshot_json,revision FROM lifecycle_state WHERE singleton=1").fetchone()
            if row is None:
                raise LifecycleConflict("Missing lifecycle state")
            snapshot = MaintenanceSnapshot.model_validate_json(row[0])
            if snapshot.revision != row[1]:
                raise LifecycleConflict("Lifecycle snapshot revision mismatch")
            return snapshot

    def save(self, previous: MaintenanceSnapshot, current: MaintenanceSnapshot) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE lifecycle_state SET revision=?,snapshot_json=? WHERE singleton=1 AND revision=?",
                (current.revision, current.model_dump_json(), previous.revision),
            ).rowcount
            if changed != 1:
                raise LifecycleConflict("Stale lifecycle revision")
            db.execute("INSERT INTO lifecycle_transitions VALUES(?,?,?,?)", (
                current.revision, previous.state.value, current.state.value, current.model_dump_json(),
            ))

    def history(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(revision=r[0], from_state=r[1], to_state=r[2]) for r in db.execute(
                "SELECT revision,from_state,to_state FROM lifecycle_transitions ORDER BY revision")]
