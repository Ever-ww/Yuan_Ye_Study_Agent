"""JSONL 会话记录、索引和恢复服务。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class SessionStore:
    """以日期加会话哈希命名 JSONL，并维护最新分段索引。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.index_path = directory / "index.json"

    def initialize(self) -> None:
        """创建会话目录及可审计索引文件。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_index({"version": 1, "sessions": {}})

    def create(self, first_message: str, session_id: str | None = None) -> str:
        """创建会话；可接收 Runtime 预生成的稳定会话标识。"""
        self.initialize()
        now = datetime.now().astimezone()
        session_id = session_id or hashlib.sha256(f"{now.isoformat()}:{first_message}:{uuid4().hex}".encode("utf-8")).hexdigest()[:16]
        if len(session_id) != 16 or any(char not in "0123456789abcdef" for char in session_id):
            raise ValueError("会话标识必须是 16 位小写十六进制字符串")
        if self.exists(session_id):
            raise ValueError(f"会话已存在：{session_id}")
        filename = f"{now:%Y-%m-%d}_{session_id}_001.jsonl"
        index = self._read_index()
        index["sessions"][session_id] = {"created_at": now.strftime("%Y-%m-%d %H:%M:%S"), "latest_file": filename, "files": [filename]}
        self._write_index(index)
        (self.directory / filename).touch()
        return session_id

    def append(self, session_id: str, role: str, content: str, metadata: dict[str, object] | None = None) -> None:
        """向当前最新 JSONL 分段追加一条带时间戳的对话消息。"""
        if role not in {"user", "assistant"}:
            raise ValueError("会话记录角色只能是 user 或 assistant")
        record = {"role": role, "content": content, "timestamp": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")}
        if metadata:
            record.update(metadata)
        path = self._active_path(session_id)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def restore(self, session_id: str) -> list[dict[str, str]]:
        """只读取索引指定的最新 JSONL，用于恢复当前上下文。"""
        records: list[dict[str, str]] = []
        for value in self.read_records(session_id):
            if value.get("role") in {"user", "assistant"} and isinstance(value.get("content"), str):
                records.append({"role": value["role"], "content": value["content"]})
        return records

    def read_records(self, session_id: str) -> list[dict[str, object]]:
        """读取最新分段的原始记录，保留时间戳供 CLI 展示。"""
        path = self._active_path(session_id)
        records: list[dict[str, object]] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"会话 {session_id} 第 {number} 行不是合法 JSON") from exc
            if isinstance(value, dict):
                records.append(value)
        return records

    def exists(self, session_id: str) -> bool:
        """判断索引中是否存在指定会话哈希。"""
        self.initialize()
        return session_id in self._read_index()["sessions"]

    def list_sessions(self) -> list[dict[str, object]]:
        """按创建时间倒序返回会话摘要和最新分段消息数。"""
        self.initialize()
        sessions: list[dict[str, object]] = []
        for session_id, metadata in self._read_index()["sessions"].items():
            path = self.directory / metadata["latest_file"]
            message_count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) if path.exists() else 0
            sessions.append({"session_id": session_id, "created_at": metadata["created_at"], "latest_file": metadata["latest_file"], "message_count": message_count})
        return sorted(sessions, key=lambda item: str(item["created_at"]), reverse=True)

    def start_new_segment(self, session_id: str) -> Path:
        """为未来上下文压缩创建同哈希的新 JSONL 分段并更新最新索引。"""
        index = self._read_index()
        session = index["sessions"].get(session_id)
        if not session:
            raise KeyError(f"未知会话：{session_id}")
        number = len(session["files"]) + 1
        date = session["files"][0].split("_", 1)[0]
        filename = f"{date}_{session_id}_{number:03d}.jsonl"
        session["files"].append(filename)
        session["latest_file"] = filename
        self._write_index(index)
        path = self.directory / filename
        path.touch()
        return path

    def _active_path(self, session_id: str) -> Path:
        """从索引定位会话最新 JSONL，未知会话明确报错。"""
        self.initialize()
        session = self._read_index()["sessions"].get(session_id)
        if not session:
            raise KeyError(f"未知会话：{session_id}")
        return self.directory / session["latest_file"]

    def _read_index(self) -> dict:
        """读取索引 JSON。"""
        self.initialize()
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _write_index(self, value: dict) -> None:
        """原子替换索引，避免中断留下半个 JSON 文件。"""
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.index_path)
