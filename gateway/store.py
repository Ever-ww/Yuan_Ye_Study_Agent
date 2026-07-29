"""Gateway SQLite 元数据与逐 Run JSONL 事件存储。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from gateway.models import (
    ApprovalRequest,
    GatewayEventEnvelope,
    InboxItem,
    ProjectRecord,
    RunRecord,
    now_iso,
)


class GatewayStore:
    """用 SQLite 管理并发元数据，用 JSONL 保留可审计事件。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.database_path = self.directory / "gateway.sqlite3"
        self.runs_directory = self.directory / "runs"
        self._lock = threading.RLock()
        self.initialize()

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        interrupted: list[dict[str, Any]] = []
        with self._connect() as connection:
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
            interrupted = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM runs WHERE status IN ('queued','running')",
                ).fetchall()
            ]
            connection.execute(
                "UPDATE runs SET status='interrupted', finished_at=? "
                "WHERE status IN ('queued','running')",
                (now_iso(),),
            )
            connection.execute(
                "UPDATE approvals SET state='denied', decided_at=? WHERE state='pending'",
                (now_iso(),),
            )
            for row in interrupted:
                connection.execute(
                    "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        row["run_id"],
                        row["project_id"],
                        row["session_id"],
                        str(row["task"])[:120],
                        "Gateway 异常退出，原模型请求无法继续",
                        "interrupted",
                        now_iso(),
                        0,
                    ),
                )
        for row in interrupted:
            self.append_event(
                str(row["run_id"]),
                str(row["project_id"]),
                str(row["session_id"]) if row["session_id"] else None,
                "run_interrupted",
                {"message": "Gateway 异常退出，原模型请求无法继续"},
            )

    def register_project(self, path: Path, name: str | None = None) -> ProjectRecord:
        resolved = path.resolve()
        if not resolved.is_dir():
            raise ValueError(f"工作区不存在或不是目录：{resolved}")
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
            project_id=project_id,
            name=selected_name,
            path=str(resolved),
            created_at=created_at,
            last_opened_at=timestamp,
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
            raise KeyError(f"未知项目：{project_id}")
        return ProjectRecord(**dict(row))

    def remove_project(self, project_id: str) -> None:
        with self._connect() as connection:
            active = connection.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running') LIMIT 1",
                (project_id,),
            ).fetchone()
            if active is not None:
                raise RuntimeError("项目仍有排队或运行中的任务，不能移除")
            cursor = connection.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
        if cursor.rowcount == 0:
            raise KeyError(f"未知项目：{project_id}")

    def create_run(
        self,
        project_id: str,
        client_id: str,
        task: str,
        session_id: str | None,
    ) -> RunRecord:
        run = RunRecord(
            run_id=uuid4().hex,
            project_id=project_id,
            session_id=session_id,
            client_id=client_id,
            task=task,
            status="queued",
            created_at=now_iso(),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.run_id, run.project_id, run.session_id, run.client_id, run.task,
                    run.status, run.created_at, None, None, None, None,
                ),
            )
            connection.execute(
                "INSERT INTO event_sequences(run_id,last_sequence) VALUES(?,0)",
                (run.run_id,),
            )
        self.event_path(run.run_id).touch()
        return run

    def update_run(self, run_id: str, **changes: Any) -> RunRecord:
        allowed = {"session_id", "status", "started_at", "finished_at", "answer", "error"}
        invalid = set(changes).difference(allowed)
        if invalid:
            raise ValueError(f"不允许更新 Run 字段：{sorted(invalid)[0]}")
        if changes:
            assignments = ",".join(f"{key}=?" for key in changes)
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE runs SET {assignments} WHERE run_id=?",
                    (*changes.values(), run_id),
                )
        return self.run(run_id)

    def run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知运行：{run_id}")
        return RunRecord(**dict(row))

    def list_runs(self, project_id: str | None = None) -> list[RunRecord]:
        query = "SELECT * FROM runs"
        parameters: tuple[str, ...] = ()
        if project_id:
            query += " WHERE project_id=?"
            parameters = (project_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [RunRecord(**dict(row)) for row in rows]

    def append_event(
        self,
        run_id: str,
        project_id: str,
        session_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> GatewayEventEnvelope:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM event_sequences WHERE run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"未知运行：{run_id}")
            sequence = int(row["last_sequence"]) + 1
            connection.execute(
                "UPDATE event_sequences SET last_sequence=? WHERE run_id=?",
                (sequence, run_id),
            )
            envelope = GatewayEventEnvelope(
                event_id=uuid4().hex,
                sequence=sequence,
                timestamp=now_iso(),
                project_id=project_id,
                session_id=session_id,
                run_id=run_id,
                type=event_type,
                payload=payload,
            )
            with self.event_path(run_id).open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(envelope.model_dump_json() + "\n")
        return envelope

    def read_events(self, run_id: str, after_sequence: int = 0) -> list[GatewayEventEnvelope]:
        path = self.event_path(run_id)
        if not path.exists():
            raise KeyError(f"未知运行：{run_id}")
        events: list[GatewayEventEnvelope] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = GatewayEventEnvelope.model_validate_json(line, strict=True)
                if event.sequence > after_sequence:
                    events.append(event)
        return events

    def create_inbox(self, run: RunRecord) -> InboxItem:
        item = InboxItem(
            item_id=uuid4().hex,
            run_id=run.run_id,
            project_id=run.project_id,
            session_id=run.session_id,
            title=run.task[:120],
            summary=run.answer or run.error or "",
            status=run.status,
            created_at=now_iso(),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO inbox VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    item.item_id, item.run_id, item.project_id, item.session_id,
                    item.title, item.summary, item.status, item.created_at, 0,
                ),
            )
        return item

    def list_inbox(self, *, unread_only: bool = False) -> list[InboxItem]:
        query = "SELECT * FROM inbox"
        if unread_only:
            query += " WHERE is_read=0"
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [
            InboxItem(
                item_id=row["item_id"],
                run_id=row["run_id"],
                project_id=row["project_id"],
                session_id=row["session_id"],
                title=row["title"],
                summary=row["summary"],
                status=row["status"],
                created_at=row["created_at"],
                read=bool(row["is_read"]),
            )
            for row in rows
        ]

    def mark_inbox_read(self, item_id: str) -> InboxItem:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE inbox SET is_read=1 WHERE item_id=?", (item_id,))
            row = connection.execute("SELECT * FROM inbox WHERE item_id=?", (item_id,)).fetchone()
        if cursor.rowcount == 0 or row is None:
            raise KeyError(f"未知 Inbox 项：{item_id}")
        return InboxItem(
            item_id=row["item_id"], run_id=row["run_id"], project_id=row["project_id"],
            session_id=row["session_id"], title=row["title"], summary=row["summary"],
            status=row["status"], created_at=row["created_at"], read=True,
        )

    def save_approval(self, request: ApprovalRequest) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)",
                (
                    request.approval_id, request.run_id, request.client_id,
                    request.tool_name, json.dumps(request.arguments, ensure_ascii=False),
                    request.state, request.created_at, request.decided_at,
                ),
            )

    def decide_approval(self, approval_id: str, approved: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE approvals SET state=?,decided_at=? WHERE approval_id=? AND state='pending'",
                ("approved" if approved else "denied", now_iso(), approval_id),
            )

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

    def event_path(self, run_id: str) -> Path:
        return self.runs_directory / f"{run_id}.jsonl"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _project_id(path: Path) -> str:
    import hashlib
    normalized = str(path.resolve()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
