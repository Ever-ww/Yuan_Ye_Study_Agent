"""会话 JSONL 与长期 Profile 的统一记忆门面。"""

from __future__ import annotations

from pathlib import Path

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

    def restore_messages(self, session_id: str) -> list[dict[str, str]]:
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

    def profile_context(self) -> str:
        """返回所有非空长期 Profile 内容。"""
        return self.profiles.load_all()
