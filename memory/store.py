"""会话 JSONL 与长期 Profile 的统一记忆门面。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .profile import ProfileStore
from .session import SessionStore


class MemoryStore:
    """项目 `.yy/memory` 下全部记忆能力的唯一入口。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sessions = SessionStore(root / "session")
        self.profiles = ProfileStore(root / "profile")
        self.initialize()

    def initialize(self) -> None:
        """确保首次运行所需目录、索引和默认 Profile 全部存在。"""
        self.sessions.initialize()
        self.profiles.initialize()

    def create_session(self, first_message: str, session_id: str | None = None) -> str:
        """创建会话并返回稳定哈希。"""
        return self.sessions.create(first_message, session_id)

    def record_user(self, session_id: str, content: str) -> None:
        """记录一条用户输入。"""
        self.sessions.append(session_id, "user", content)

    def record_assistant(
        self,
        session_id: str,
        content: str,
        *,
        model: dict[str, object] | None = None,
        model_calls: list[dict[str, object]] | None = None,
        task_latency_ms: float | None = None,
    ) -> None:
        """记录最终助手回复，以及本次用户任务的模型、时延和 Token 指标。"""
        metadata: dict[str, object] = {}
        if model is not None:
            metadata["model"] = model
        if model_calls is not None:
            metadata["model_calls"] = model_calls
        if task_latency_ms is not None:
            metadata["task_latency_ms"] = task_latency_ms
        self.sessions.append(session_id, "assistant", content, metadata)

    def record_model_tool_calls(
        self,
        session_id: str,
        *,
        content: str | None,
        tool_calls: list[dict[str, Any]],
        model: dict[str, object],
        model_call: dict[str, object],
    ) -> None:
        """记录模型原始返回的标准 assistant.tool_calls 消息。"""
        self.sessions.append(session_id, "assistant", content, {
            "tool_calls": tool_calls,
            "model": model,
            "model_call": model_call,
        })

    def record_tool_result(
        self,
        session_id: str,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        status: str,
        arguments: dict[str, Any],
    ) -> None:
        """记录工具成功结果或错误反馈。"""
        self.sessions.append(session_id, "tool", content, {
            "tool_call_id": tool_call_id,
            "name": name,
            "status": status,
            "arguments": arguments,
        })

    def restore_messages(self, session_id: str) -> list[dict[str, Any]]:
        """恢复索引指向的最新会话分段。"""
        return self.sessions.restore(session_id)

    def has_session(self, session_id: str) -> bool:
        """判断会话哈希是否可恢复。"""
        return self.sessions.exists(session_id)

    def list_sessions(self) -> list[dict[str, object]]:
        """返回供 CLI 展示的会话摘要。"""
        return self.sessions.list_sessions()

    def session_records(self, session_id: str) -> list[dict[str, object]]:
        """读取带时间戳的原始会话记录。"""
        return self.sessions.read_records(session_id)

    def profile_context(self, session_id: str | None = None) -> str:
        """返回全局 Profile 与指定会话独占的哈希 Profile。"""
        return self.profiles.load_for_session(session_id)

    def active_filename(self, session_id: str) -> str:
        """返回会话当前 JSONL 文件名。"""
        return self.sessions.active_filename(session_id)

    def rollover_with_summary(self, session_id: str, summary: str, source_file: str) -> Path:
        """创建以 summary 记录开头的新会话分段。"""
        return self.sessions.rollover(session_id, [{
            "role": "summary",
            "content": summary,
            "source_file": source_file,
        }])

    def commit_compression(
        self,
        session_id: str,
        *,
        profile_markdown: str,
        context_summary: str,
        source_file: str,
        conversation_turns: int,
        records_processed: int,
        tool_calls_processed: int,
    ) -> tuple[Path, Path]:
        """协调 Profile 与新分段写入；切段失败时恢复旧 Profile 状态。"""
        profile_path = self.profiles.directory / f"{session_id}.md"
        profile_backup = profile_path.read_bytes() if profile_path.exists() else None
        index_backup = self.profiles.index_path.read_bytes() if self.profiles.index_path.exists() else None
        try:
            committed_profile = self.profiles.commit_session_profile(
                session_id,
                profile_markdown,
                source_file=source_file,
                conversation_turns=conversation_turns,
                records_processed=records_processed,
                tool_calls_processed=tool_calls_processed,
            )
            segment = self.rollover_with_summary(session_id, context_summary, source_file)
            return committed_profile, segment
        except Exception:
            if profile_backup is None:
                profile_path.unlink(missing_ok=True)
            else:
                profile_path.write_bytes(profile_backup)
            if index_backup is not None:
                self.profiles.index_path.write_bytes(index_backup)
            raise
