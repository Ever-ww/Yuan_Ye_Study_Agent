"""Gateway metadata queries and non-domain projections.

Run lifecycle, Approval and durable Gateway Event mutations deliberately do not
live here.  ``StateController`` is their only write authority; this store retains
project/client metadata and read-only Run/Inbox projections.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from gateway.models import InboxItem, ProjectRecord, RunRecord, now_iso


class GatewayStore:
    """Query Gateway projections and mutate only independent metadata."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.database_path = self.directory / "gateway.sqlite3"
        self.runs_directory = self.directory / "runs"
        self.backups_directory = self.directory / "backups"
        self.migration_backup_path: Path | None = None
        self._lock = threading.RLock()
        self._backup_before_migration()
        self.initialize()

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if check != "ok":
                raise RuntimeError(f"Gateway SQLite quick_check failed: {check}")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, last_opened_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT,
                    client_id TEXT NOT NULL, task TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    answer TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    item_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL, session_id TEXT, title TEXT NOT NULL,
                    summary TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, client_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS event_sequences (
                    run_id TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY, connected_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, disconnected_at TEXT
                );
                """
            )
            self._ensure_run_columns(connection)

    def register_project(self, path: Path, name: str | None = None) -> ProjectRecord:
        resolved = path.resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not a directory: {resolved}")
        project_id = _project_id(resolved)
        timestamp = now_iso()
        selected_name = (name or resolved.name or str(resolved)).strip()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM projects WHERE project_id=?", (project_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else timestamp
            connection.execute(
                "INSERT INTO projects(project_id,name,path,created_at,last_opened_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET name=excluded.name,path=excluded.path,"
                "last_opened_at=excluded.last_opened_at",
                (project_id, selected_name, str(resolved), created_at, timestamp),
            )
        return ProjectRecord(
            project_id=project_id, name=selected_name, path=str(resolved),
            created_at=created_at, last_opened_at=timestamp,
        )

    def list_projects(self) -> list[ProjectRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY last_opened_at DESC",
            ).fetchall()
        return [ProjectRecord(**dict(row)) for row in rows]

    def project(self, project_id: str) -> ProjectRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")
        return ProjectRecord(**dict(row))

    def remove_project(self, project_id: str) -> None:
        with self._connect() as connection:
            active = connection.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running') LIMIT 1",
                (project_id,),
            ).fetchone()
            if active is not None:
                raise RuntimeError("Project still has an active Run")
            cursor = connection.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown project: {project_id}")

    def run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown Run: {run_id}")
        return _run_record(row)

    def list_runs(self, project_id: str | None = None) -> list[RunRecord]:
        query = "SELECT * FROM runs"
        parameters: tuple[str, ...] = ()
        if project_id:
            query += " WHERE project_id=?"
            parameters = (project_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_run_record(row) for row in rows]

    def automated_session_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM runs WHERE session_id IS NOT NULL "
                "AND (client_id LIKE 'cron:%' OR client_id LIKE 'dream:%')",
            ).fetchall()
        return {str(row["session_id"]) for row in rows}

    def list_inbox(self, *, unread_only: bool = False) -> list[InboxItem]:
        query = "SELECT * FROM inbox"
        if unread_only:
            query += " WHERE is_read=0"
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [_inbox_item(row) for row in rows]

    def mark_inbox_read(self, item_id: str) -> InboxItem:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE inbox SET is_read=1 WHERE item_id=?", (item_id,))
            row = connection.execute("SELECT * FROM inbox WHERE item_id=?", (item_id,)).fetchone()
        if cursor.rowcount == 0 or row is None:
            raise KeyError(f"Unknown Inbox item: {item_id}")
        return _inbox_item(row)

    def mark_run_inbox_read(self, run_id: str) -> InboxItem | None:
        with self._connect() as connection:
            connection.execute("UPDATE inbox SET is_read=1 WHERE run_id=?", (run_id,))
            row = connection.execute("SELECT * FROM inbox WHERE run_id=?", (run_id,)).fetchone()
        return _inbox_item(row) if row is not None else None

    def client_connected(self, client_id: str) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO clients(client_id,connected_at,last_seen_at,disconnected_at) "
                "VALUES(?,?,?,NULL) ON CONFLICT(client_id) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,disconnected_at=NULL",
                (client_id, timestamp, timestamp),
            )

    def client_disconnected(self, client_id: str) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                "UPDATE clients SET last_seen_at=?,disconnected_at=? WHERE client_id=?",
                (timestamp, timestamp, client_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _backup_before_migration(self, target_version: int = 3) -> None:
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return
        with sqlite3.connect(self.database_path, timeout=30) as source:
            current = int(source.execute("PRAGMA user_version").fetchone()[0])
            if current >= target_version:
                return
            self.backups_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
            backup = self.backups_directory / f"gateway-v{current}-to-v{target_version}-{stamp}.sqlite3"
            with sqlite3.connect(backup) as target:
                source.backup(target)
            self.migration_backup_path = backup

    @staticmethod
    def _ensure_run_columns(connection: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
        additions = {
            "task_state": "TEXT", "execution_state": "TEXT", "execution_outcome": "TEXT",
            "finish_reason": "TEXT", "state_revision": "INTEGER NOT NULL DEFAULT 0",
            "workload_kind": "TEXT NOT NULL DEFAULT 'chat'",
            "recovery_required": "INTEGER NOT NULL DEFAULT 0", "terminal_target": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")


def _project_id(path: Path) -> str:
    import hashlib
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:16]


def _run_record(row: sqlite3.Row) -> RunRecord:
    payload = dict(row)
    if "recovery_required" in payload:
        payload["recovery_required"] = bool(payload["recovery_required"])
    return RunRecord(**payload)


def _inbox_item(row: sqlite3.Row) -> InboxItem:
    return InboxItem(
        item_id=row["item_id"], run_id=row["run_id"], project_id=row["project_id"],
        session_id=row["session_id"], title=row["title"], summary=row["summary"],
        status=row["status"], created_at=row["created_at"], read=bool(row["is_read"]),
    )
