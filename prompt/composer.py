"""单一 System Prompt 与当前任务 Prompt 的组合服务。"""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from Agent.config import RuntimeConfig
    from memory import MemoryStore
    from sandbox import SandboxStatus
    from skill import SkillService


class SystemPromptSnapshot(BaseModel):
    """一个 Session 缓存的唯一 System Prompt。"""

    model_config = ConfigDict(frozen=True, strict=True)

    session_id: str
    segment_path: Path
    initialized_at: str
    content: str


class SystemPromptComposer:
    """只在缓存创建或显式刷新时读取本机上下文文件。"""

    def __init__(
        self,
        config: "RuntimeConfig",
        memory: "MemoryStore",
        skills: "SkillService | None" = None,
        sandbox_enabled: bool = False,
    ) -> None:
        self.config = config
        self.memory = memory
        self.skills = skills
        self.sandbox_mode = "docker" if sandbox_enabled else "closed"
        self._snapshots: dict[str, SystemPromptSnapshot] = {}

    def set_sandbox_status(self, status: "SandboxStatus") -> None:
        """在 Trace 探测完成后固定当前 Session 的实际安全能力。"""
        if self.sandbox_mode != status.mode:
            self._snapshots.clear()
        self.sandbox_mode = status.mode

    def open_session(self, session_id: str, *, force: bool = False) -> SystemPromptSnapshot:
        """返回 Session 的缓存快照；仅 force 时重新读取文件。"""
        if not force and session_id in self._snapshots:
            return self._snapshots[session_id]
        initialized_at = self.memory.session_created_at(session_id)
        segment_path = self.memory.active_path(session_id)
        skill_xml = self.skills.catalog_xml() if self.skills is not None else "<available_skills></available_skills>"
        soul = _read(self.config.agent_root / ".yy" / "agents" / "SOUL.md")
        agent = _read(self.config.agent_root / ".yy" / "agents" / "AGENT.md")
        profile = self.memory.prompt_context(session_id)
        summary = self.memory.latest_summary(session_id)
        environment = _environment(self.config, sandbox_mode=self.sandbox_mode)
        sections = [
            skill_xml,
            "# Agent 身份（SOUL）\n" + soul,
            (
                "# 核心规则\n你是严谨、透明的本地 Agent。工具调用必须遵守权限、工作区边界和审批要求。"
                "网络调研中，web_search 只用于发现候选 URL；需要读取正文或核验摘要时，"
                "应从搜索结果选择相关 URL 继续调用 web_fetch。"
            ),
            "# 项目说明（AGENT）\n" + agent,
            "# 长时记忆\n" + (profile or "（暂无可用长期记忆）"),
            "# 系统与会话信息\n" + environment + (
                f"\nSession ID：{session_id}\n当前分段：{segment_path.name}\n分段绝对路径：{segment_path}"
                f"\nSession 初始化时间：{initialized_at}"
            ),
        ]
        if summary:
            sections.append("# 当前会话压缩摘要\n" + summary)
        snapshot = SystemPromptSnapshot(
            session_id=session_id,
            segment_path=segment_path,
            initialized_at=initialized_at,
            content="\n\n".join(sections),
        )
        self._snapshots[session_id] = snapshot
        return snapshot

    def discard(self, session_id: str) -> None:
        self._snapshots.pop(session_id, None)


class TaskPromptComposer:
    """当前用户消息只在发送模型时附加本地时间。"""

    def compose(self, task: str) -> dict[str, str]:
        now = datetime.now().astimezone()
        return {
            "role": "user",
            "content": f"{task}\n\n[本次提问时间：{now:%Y-%m-%d %H:%M:%S}，时区：{now.tzname() or str(now.tzinfo)}]",
        }


class PromptComposer:
    """兼容 Runtime 使用的 Prompt 门面，System Prompt 始终是一个字符串。"""

    def __init__(self, config: "RuntimeConfig | Path", memory: "MemoryStore | SkillService | None" = None, skills: "SkillService | None" = None, sandbox_enabled: bool = False) -> None:
        # 兼容独立 Prompt 测试：PromptComposer(project_root, skill_service)。
        if isinstance(config, Path):
            from Agent.config import load_runtime_config
            from memory import MemoryStore
            root = config
            if skills is None and memory is not None and not isinstance(memory, MemoryStore):
                skills = memory  # type: ignore[assignment]
                memory = None
            config = load_runtime_config(root)
            memory = memory if isinstance(memory, MemoryStore) else MemoryStore(config.memory_dir)
        if memory is None:
            raise ValueError("PromptComposer 需要 MemoryStore")
        self.system = SystemPromptComposer(config, memory, skills, sandbox_enabled=sandbox_enabled)
        self.task = TaskPromptComposer()

    def compose(self, task: str, session_id: str | None = None) -> list[dict[str, str]]:
        if session_id is None:
            # 独立使用时没有会话索引，只返回缓存前的系统来源与任务消息。
            skill_xml = self.system.skills.catalog_xml() if self.system.skills is not None else "<available_skills></available_skills>"
            content = "\n\n".join([skill_xml, "# Agent 身份（SOUL）\n（独立 Prompt 模式）"])
            return [{"role": "system", "content": content}, self.task.compose(task)]
        snapshot = self.system.open_session(session_id)
        return [{"role": "system", "content": snapshot.content}, self.task.compose(task)]

    def refresh(self, session_id: str) -> SystemPromptSnapshot:
        return self.system.open_session(session_id, force=True)

    def close(self, session_id: str) -> None:
        self.system.discard(session_id)

    def set_sandbox_status(self, status: "SandboxStatus") -> None:
        self.system.set_sandbox_status(status)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else "（未配置）"


def _environment(config: "RuntimeConfig", *, sandbox_mode: str) -> str:
    now = datetime.now().astimezone()
    sandbox_text = {
        "docker": "Docker 沙箱（Bash 可用，文件修改受 checkpoint 保护）",
        "checkpoint_only": "Checkpoint-only（Bash 禁用，本地写入与回溯可用）",
        "pending": "正在探测 Docker",
        "closed": "未启用或已关闭",
    }.get(sandbox_mode, sandbox_mode)
    return (
        f"操作系统：{platform.system()} {platform.release()}\n"
        f"架构：{platform.machine()}\nPython：{platform.python_version()}\n"
        f"工作区：{config.workspace_root}\nSandbox：{sandbox_text}\n"
        f"时区：{now.tzname() or str(now.tzinfo)}\n系统时间：{now:%Y-%m-%d %H:%M:%S}"
    )
