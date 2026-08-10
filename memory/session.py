"""JSONL 会话记录、索引和恢复服务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .models import SessionIndex, SessionRecord


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
        path = self.directory / filename
        self._publish_segment(path, [])
        index = self._read_index()
        index["sessions"][session_id] = {"created_at": now.strftime("%Y-%m-%d %H:%M:%S"), "latest_file": filename, "files": [filename]}
        try:
            self._write_index(index)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return session_id

    def append(self, session_id: str, role: str, content: str | None, metadata: dict[str, object] | None = None) -> str:
        """向当前最新 JSONL 分段追加一条带时间戳的对话消息。"""
        record = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "record_id": uuid4().hex,
        }
        if metadata:
            record.update(metadata)
        record_id = str(record["record_id"])
        self.append_once(session_id, record)
        return record_id

    def append_once(self, session_id: str, record: dict[str, Any]) -> bool:
        """Append by stable record ID and reject conflicting duplicate content."""
        selected = dict(record)
        selected.setdefault("record_id", uuid4().hex)
        record_id = str(selected["record_id"])
        if not record_id:
            raise ValueError("Session record_id cannot be empty")
        existing = self.record_by_id_strict(session_id, record_id)
        if existing is not None:
            # A retry may reconstruct the same logical record at a later wall
            # clock time.  The stable record ID owns the original timestamp;
            # every other persisted field must still match exactly.
            selected["timestamp"] = existing.timestamp
            expected = SessionRecord.model_validate(selected).model_dump(
                mode="python", exclude_unset=True,
            )
            actual = existing.model_dump(mode="python", exclude_unset=True)
            if self._canonical_record(expected) != self._canonical_record(actual):
                raise ValueError(f"Session record_id content conflict: {record_id}")
            return False
        selected.setdefault(
            "timestamp", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        )
        validated = SessionRecord.model_validate(selected).model_dump(
            mode="python", exclude_unset=True,
        )
        path = self._active_path(session_id)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(validated, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def record_by_id_strict(self, session_id: str, record_id: str) -> SessionRecord | None:
        for _, record in self.read_all_records_strict(session_id):
            if record.record_id == record_id:
                return record
        return None

    def read_all_records_strict(self, session_id: str) -> list[tuple[str, SessionRecord]]:
        """Strictly parse every indexed segment in index order."""
        entry = self.index_entry(session_id)
        selected: list[tuple[str, SessionRecord]] = []
        for filename in entry["files"]:
            path = self.directory / str(filename)
            if not path.is_file():
                raise ValueError(f"Session index references missing segment: {filename}")
            with path.open("r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = SessionRecord.model_validate_json(line, strict=True)
                    except ValidationError as exc:
                        raise ValueError(
                            f"Session {session_id} segment {filename} line {number} is invalid: {exc}",
                        ) from exc
                    selected.append((str(filename), record))
        return selected

    def find_records_strict(
        self,
        session_id: str,
        *,
        run_id: str,
        turn_id: str | None,
    ) -> list[tuple[str, SessionRecord]]:
        return [
            (filename, record)
            for filename, record in self.read_all_records_strict(session_id)
            if record.run_id == run_id and (turn_id is None or record.turn_id == turn_id)
        ]

    def index_entry(self, session_id: str) -> dict[str, Any]:
        session = self._read_index()["sessions"].get(session_id)
        if session is None:
            raise KeyError(f"Unknown Session: {session_id}")
        return dict(session)

    @staticmethod
    def _canonical_record(record: dict[str, Any]) -> str:
        return json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )

    def restore(self, session_id: str) -> list[dict[str, Any]]:
        """恢复最新分段并移除时间戳、模型指标等审计字段。"""
        records: list[dict[str, Any]] = []
        for value in self.read_records(session_id):
            role, content = value.get("role"), value.get("content")
            if role == "user" and isinstance(content, str):
                records.append({"role": "user", "content": content})
            elif role == "assistant" and (isinstance(content, str) or content is None):
                message: dict[str, Any] = {"role": "assistant", "content": content}
                if isinstance(value.get("tool_calls"), list):
                    message["tool_calls"] = value["tool_calls"]
                if content is not None or "tool_calls" in message:
                    records.append(message)
            elif role == "tool" and isinstance(content, str):
                call_id, name = value.get("tool_call_id"), value.get("name")
                if isinstance(call_id, str) and isinstance(name, str):
                    records.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
        return _normalize_recovered_history(_complete_incomplete_tool_chains(records))

    def latest_summary(self, session_id: str) -> str:
        """读取当前分段首条摘要；摘要由唯一 System Prompt 承载。"""
        for record in self.read_records(session_id):
            if record.get("role") == "summary" and isinstance(record.get("content"), str):
                return str(record["content"])
        return ""
    def read_records(self, session_id: str) -> list[dict[str, object]]:
        """读取最新分段的原始记录，保留时间戳供 CLI 展示。"""
        path = self._active_path(session_id)
        records: list[dict[str, object]] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = SessionRecord.model_validate_json(line).model_dump(mode="python", exclude_unset=True)
            except ValidationError as exc:
                raise ValueError(f"会话 {session_id} 第 {number} 行格式无效：{exc}") from exc
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
        return self.rollover(session_id, [])

    def rollover(
        self,
        session_id: str,
        initial_records: list[dict[str, Any]],
        *,
        skill_catalog: dict[str, Any] | None = None,
    ) -> Path:
        """先完整写入新分段，再原子切换会话索引的 latest_file。"""
        index = self._read_index()
        session = index["sessions"].get(session_id)
        if not session:
            raise KeyError(f"未知会话：{session_id}")
        number = len(session["files"]) + 1
        date = session["files"][0].split("_", 1)[0]
        filename = f"{date}_{session_id}_{number:03d}.jsonl"
        path = self.directory / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        lines = []
        for record in initial_records:
            value = dict(record)
            value.setdefault("timestamp", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"))
            validated = SessionRecord.model_validate(value).model_dump(mode="python", exclude_unset=True)
            lines.append(json.dumps(validated, ensure_ascii=False))
        self._write_file_durable(temporary, ("\n".join(lines) + "\n") if lines else "")
        os.replace(temporary, path)
        self._fsync_directory(path.parent)
        session["files"].append(filename)
        session["latest_file"] = filename
        if skill_catalog is not None:
            session["skill_catalog"] = skill_catalog
        self._write_index(index)
        return path

    def skill_catalog(self, session_id: str) -> dict[str, Any] | None:
        """返回 Session 持久化的 Skill 快照；旧 Session 可能暂无此字段。"""
        session = self._read_index()["sessions"].get(session_id)
        if session is None:
            raise KeyError(f"未知会话：{session_id}")
        value = session.get("skill_catalog")
        return dict(value) if isinstance(value, dict) else None

    def set_skill_catalog(self, session_id: str, catalog: dict[str, Any]) -> None:
        """为新 Session 原子记录初始 Skill 快照。"""
        index = self._read_index()
        session = index["sessions"].get(session_id)
        if session is None:
            raise KeyError(f"未知会话：{session_id}")
        session["skill_catalog"] = dict(catalog)
        self._write_index(index)

    def active_filename(self, session_id: str) -> str:
        """返回索引指向的当前分段文件名。"""
        return self._active_path(session_id).name

    def active_path(self, session_id: str) -> Path:
        """返回当前分段的绝对路径，供 System Prompt 审计说明使用。"""
        return self._active_path(session_id)

    def created_at(self, session_id: str) -> str:
        """返回索引记录的 Session 初始化时间。"""
        session = self._read_index()["sessions"].get(session_id)
        if not session:
            raise KeyError(f"未知会话：{session_id}")
        return str(session["created_at"])

    @staticmethod
    def is_session_hash(value: str) -> bool:
        """判断是否为 Runtime 使用的 16 位小写十六进制会话标识。"""
        return re.fullmatch(r"[0-9a-f]{16}", value) is not None

    def _active_path(self, session_id: str) -> Path:
        """从索引定位会话最新 JSONL，未知会话明确报错。"""
        self.initialize()
        session = self._read_index()["sessions"].get(session_id)
        if not session:
            raise KeyError(f"未知会话：{session_id}")
        return self.directory / session["latest_file"]

    def _read_index(self) -> dict:
        """读取并校验索引 JSON。"""
        self.initialize()
        return SessionIndex.model_validate_json(
            self.index_path.read_text(encoding="utf-8"), strict=True,
        ).model_dump(mode="python")

    def _publish_segment(self, path: Path, records: list[dict[str, Any]]) -> None:
        lines: list[str] = []
        for record in records:
            value = SessionRecord.model_validate(record).model_dump(
                mode="python", exclude_unset=True,
            )
            lines.append(json.dumps(value, ensure_ascii=False))
        temporary = path.with_suffix(path.suffix + ".tmp")
        self._write_file_durable(temporary, ("\n".join(lines) + "\n") if lines else "")
        os.replace(temporary, path)
        self._fsync_directory(path.parent)

    @staticmethod
    def _write_file_durable(path: Path, content: str) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_index(self, value: dict) -> None:
        """原子替换索引，避免中断留下半个 JSON 文件。"""
        validated = SessionIndex.model_validate(value, strict=True)
        temporary = self.index_path.with_suffix(".tmp")
        self._write_file_durable(temporary, validated.model_dump_json(indent=2) + "\n")
        os.replace(temporary, self.index_path)
        self._fsync_directory(self.index_path.parent)


def _complete_incomplete_tool_chains(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只修复模型上下文投影，不篡改原始 JSONL 中未知的工具执行事实。"""
    completed: list[dict[str, Any]] = []
    pending: dict[str, str] = {}

    def close_pending() -> None:
        for call_id, name in pending.items():
            completed.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": (
                    "此前运行中断，未找到该工具调用的结果记录；"
                    "执行结果未知，不得假定工具已经执行或尚未执行。"
                ),
            })
        pending.clear()

    for message in records:
        role = message.get("role")
        if pending and role != "tool":
            close_pending()
        completed.append(message)
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            pending.clear()
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                function = call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(call_id, str) and isinstance(name, str):
                    pending[call_id] = name
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str):
                pending.pop(call_id, None)
    close_pending()
    return completed


def _normalize_recovered_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为旧版异常中断记录建立合法投影，避免连续 user 或 tool 后直接进入新任务。"""
    normalized: list[dict[str, Any]] = []
    marker = {
        "role": "assistant",
        "content": "此前回答在运行期间中断，没有产生可恢复的最终答复。",
    }
    for message in records:
        if message.get("role") == "user" and normalized:
            previous_role = normalized[-1].get("role")
            if previous_role in {"user", "tool"}:
                normalized.append(dict(marker))
        normalized.append(message)
    if normalized and normalized[-1].get("role") == "user":
        normalized.append(dict(marker))
    return normalized
