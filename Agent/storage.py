"""供 CLI、Web、运行时和后台调度器共享的 SQLite 状态与事件存储。

会话采用事件溯源：``sessions`` 保存当前元数据，``events`` 追加完整过程，其他表保存
文件回滚、权限、记忆、资料库、Cron、团队和插件等专用状态。连接启用外键与 WAL；
进程内使用可重入锁串行事务，避免同一 ``StateStore`` 被异步任务和线程同时写入。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .types import EventType, RunEvent, Session, utc_now


# 所有 ``CREATE`` 均为幂等操作，因此每个前端都可独立构造 ``StateStore``。
# FTS5 是项目的基础召回后端；初始化失败会直接暴露环境不满足要求，而不是无声降级。
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, project_root TEXT NOT NULL, profile TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE TABLE IF NOT EXISTS file_changes (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, event_seq INTEGER,
  path TEXT NOT NULL, before_blob BLOB, after_blob BLOB,
  before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, reverted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permission_rules (
  id TEXT PRIMARY KEY, effect TEXT NOT NULL, scope TEXT NOT NULL, project_root TEXT,
  tool TEXT NOT NULL, specifier_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
  confidence REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED, content, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS corpus_documents (
  id TEXT PRIMARY KEY, path TEXT NOT NULL, file_hash TEXT NOT NULL, title TEXT NOT NULL,
  indexed_at TEXT NOT NULL, UNIQUE(path, file_hash)
);
CREATE TABLE IF NOT EXISTS corpus_chunks (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
  page INTEGER, section TEXT, content TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(id UNINDEXED, content, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, cron TEXT NOT NULL, prompt TEXT NOT NULL, timezone TEXT NOT NULL,
  recurring INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'ready',
  next_run TEXT, last_run TEXT, expires_at TEXT, capability_json TEXT NOT NULL,
  session_id TEXT, retries INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_tasks (
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
  status TEXT NOT NULL, owner TEXT, dependencies_json TEXT NOT NULL, result TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_tasks_team ON team_tasks(team_id, status);
CREATE TABLE IF NOT EXISTS run_tasks (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  title TEXT NOT NULL, status TEXT NOT NULL, dependencies_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailboxes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL, sender TEXT NOT NULL,
  recipient TEXT NOT NULL, message TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plugin_state (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, source TEXT NOT NULL, version TEXT,
  commit_sha TEXT, content_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  trusted_components_json TEXT NOT NULL, installed_path TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class StateStore:
    """SQLite 的窄封装，统一事务、行映射和 Harness 领域操作。"""

    def __init__(self, path: str | Path) -> None:
        """创建父目录并幂等初始化全部表与索引。"""

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connection() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """提供一次自动提交、异常时自动回滚关闭的数据库连接。

        ``RLock`` 允许上层方法在同一线程安全嵌套内部操作，同时防止一个实例的多个
        线程交错事务。跨进程并发由 SQLite WAL 和 30 秒 busy timeout 协调。
        """

        with self._lock:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

    def create_session(self, project_root: str, profile: str, title: str = "") -> Session:
        """创建活动会话，并返回与数据库行一致的元数据对象。"""

        now = utc_now()
        session = Session(uuid4().hex, project_root, profile, title, now, now)
        with self.connection() as db:
            db.execute(
                "INSERT INTO sessions(id,project_root,profile,title,status,summary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (session.id, project_root, profile, title, session.status, session.summary, now, now),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        """按 ID 获取会话；不存在时返回 ``None`` 而非抛出异常。"""

        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return Session(**dict(row)) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """按最近更新时间倒序列出会话摘要。"""

        with self.connection() as db:
            rows = db.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def update_session(self, session_id: str, **values: Any) -> None:
        """仅更新允许的元数据字段，并强制刷新 ``updated_at``。

        字段名来自固定白名单，SQL 列名不会受外部输入控制；未知字段被忽略以防调用方
        越权改写 ``id`` 或 ``project_root`` 等身份属性。
        """

        allowed = {"title", "status", "summary", "updated_at"}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["updated_at"] = utc_now()
        fields = ",".join(f"{key}=?" for key in clean)
        with self.connection() as db:
            db.execute(f"UPDATE sessions SET {fields} WHERE id=?", (*clean.values(), session_id))

    def append_event(self, event: RunEvent) -> int:
        """原子追加事件并触碰会话更新时间，返回单调递增序号。"""

        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        with self.connection() as db:
            cursor = db.execute(
                "INSERT INTO events(id,session_id,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (event.id, event.session_id, event_type, json.dumps(event.payload, ensure_ascii=False), event.created_at),
            )
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (event.created_at, event.session_id))
            return int(cursor.lastrowid)

    def events(self, session_id: str, *, after_seq: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """按序号正序读取事件，并把 JSON payload 还原为字典。"""

        query = "SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq"
        params: list[Any] = [session_id, after_seq]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self.connection() as db:
            rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            result.append(value)
        return result

    def delete_events_after(self, session_id: str, seq: int) -> None:
        """删除回滚点之后的会话事件；文件恢复由调用者先行完成。"""

        with self.connection() as db:
            db.execute("DELETE FROM events WHERE session_id=? AND seq>?", (session_id, seq))

    @staticmethod
    def content_hash(value: bytes | None) -> str:
        """计算文件快照哈希；不存在的文件以空字节表示。"""

        return hashlib.sha256(value or b"").hexdigest()

    def record_file_change(self, session_id: str, path: str, before: bytes | None, after: bytes | None) -> str:
        """记录文件变更前后内容、哈希及其最近事件序号。

        同时保存字节快照而不仅是差异补丁，使回滚不依赖 Git，也能精确还原二进制
        文件。调用者应只记录 Agent 自己完成的写入。
        """

        change_id = uuid4().hex
        with self.connection() as db:
            seq = db.execute("SELECT MAX(seq) FROM events WHERE session_id=?", (session_id,)).fetchone()[0]
            db.execute(
                "INSERT INTO file_changes VALUES(?,?,?,?,?,?,?,?,?,?)",
                (change_id, session_id, seq, path, before, after, self.content_hash(before), self.content_hash(after), 0, utc_now()),
            )
        return change_id

    def file_changes_after(self, session_id: str, seq: int) -> list[dict[str, Any]]:
        """倒序返回回滚点后的未撤销文件变更，便于从新到旧逐步复原。"""

        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM file_changes WHERE session_id=? AND COALESCE(event_seq,0)>? AND reverted=0 ORDER BY created_at DESC",
                (session_id, seq),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_change_reverted(self, change_id: str) -> None:
        """把变更标记为已撤销，防止下一次 rewind 重复应用。"""

        with self.connection() as db:
            db.execute("UPDATE file_changes SET reverted=1 WHERE id=?", (change_id,))

    def commit_rewind(
        self,
        session_id: str,
        to_seq: int,
        change_ids: list[str],
        event: RunEvent,
    ) -> int:
        """在一个 SQLite 事务中提交 rewind 的变更标记、事件截断、摘要和审计事件。

        文件系统恢复由 Runtime 完成；只有所有目标文件都写入成功后才调用本方法。任一 SQL
        失败会随连接关闭整体回滚，避免出现“部分变更已标记、部分事件仍保留”的状态。
        """

        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        with self.connection() as db:
            db.executemany(
                "UPDATE file_changes SET reverted=1 WHERE id=?",
                ((change_id,) for change_id in change_ids),
            )
            db.execute("DELETE FROM events WHERE session_id=? AND seq>?", (session_id, to_seq))
            compacted = db.execute(
                "SELECT payload_json FROM events WHERE session_id=? AND type=? ORDER BY seq DESC LIMIT 1",
                (session_id, EventType.COMPACTED.value),
            ).fetchone()
            summary = ""
            if compacted:
                summary = str(json.loads(compacted["payload_json"]).get("summary", ""))
            cursor = db.execute(
                "INSERT INTO events(id,session_id,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (
                    event.id,
                    session_id,
                    event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at,
                ),
            )
            db.execute(
                "UPDATE sessions SET summary=?,updated_at=? WHERE id=?",
                (summary, event.created_at, session_id),
            )
            return int(cursor.lastrowid)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """供领域存储复用的参数化单语句写接口。"""

        with self.connection() as db:
            db.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """执行参数化查询并将 ``sqlite3.Row`` 转为普通可修改字典。"""

        with self.connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
