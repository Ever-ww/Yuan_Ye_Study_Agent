"""Non-persistent memory facade for unattended and delegated runtimes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from memory import MemoryStore


class DurableIsolatedMemory(MemoryStore):
    """Persist one workload Session without exposing long-term Profile memory."""

    runtime_notice = (
        "这是一次独立且可持久化审计的无记忆 Cron 执行。不得读取或假设长期 Profile、"
        "其他 Session 或先前 Cron 周期的内容；当前任务所需事实必须在本次执行中取得。"
    )

    def profile_context(self, session_id: str | None = None) -> str:
        del session_id
        return ""

    def prompt_context(self, session_id: str | None = None) -> str:
        del session_id
        return ""


class EphemeralMemory:
    """Satisfy Runtime/Prompt contracts without reading or writing user memory."""

    runtime_notice = (
        "这是一次独立的无记忆后台子 Agent 执行。不得读取或假设任何历史会话；"
        "当前任务所需事实必须在本次执行中通过明确提供的任务文本或工具重新取得。"
    )

    def __init__(self, agent_root: Path) -> None:
        self.agent_root = agent_root.resolve()
        self.workspace_root = self.agent_root

    def has_session(self, session_id: str) -> bool:
        return True

    def session_created_at(self, session_id: str) -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def active_path(self, session_id: str) -> Path:
        return self.agent_root / ".yy" / "runtime" / "ephemeral.jsonl"

    def prompt_context(self, session_id: str | None = None) -> str:
        return ""

    def latest_summary(self, session_id: str) -> str:
        return ""

    def session_skill_catalog(self, session_id: str):
        return None

    def set_session_skill_catalog(self, session_id: str, catalog) -> None:
        return None
