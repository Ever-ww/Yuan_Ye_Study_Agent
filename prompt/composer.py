"""单一 System Prompt 与当前任务 Prompt 的组合服务。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from skill import SkillCatalogSnapshot
from .runtime_context import AgentDynamicContextBuilder

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
    skill_catalog: SkillCatalogSnapshot | None = None


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
        self.rebuild_count = 0

    def set_sandbox_status(self, status: "SandboxStatus") -> None:
        """在 Trace 探测完成后固定当前 Session 的实际安全能力。"""
        self.sandbox_mode = status.mode

    def open_session(
        self,
        session_id: str,
        *,
        force: bool = False,
        skill_catalog: SkillCatalogSnapshot | None = None,
    ) -> SystemPromptSnapshot:
        """返回 Session 的缓存快照；仅 force 时重新读取文件。"""
        if not force and session_id in self._snapshots:
            return self._snapshots[session_id]
        initialized_at = self.memory.session_created_at(session_id)
        segment_path = self.memory.active_path(session_id)
        persisted_catalog = (
            self.memory.session_skill_catalog(session_id)
            if self.skills is not None
            and callable(getattr(self.memory, "session_skill_catalog", None))
            else None
        )
        selected_catalog = skill_catalog
        if selected_catalog is None and persisted_catalog is not None:
            selected_catalog = SkillCatalogSnapshot.model_validate(
                persisted_catalog,
            )
        if selected_catalog is None and self.skills is not None:
            selected_catalog = self.skills.catalog_snapshot()
        skill_xml = (
            self.skills.catalog_xml(selected_catalog)
            if self.skills is not None and selected_catalog is not None
            else "<available_skills></available_skills>"
        )
        soul = _read(self.config.agent_root / ".yy" / "agents" / "SOUL.md")
        agent = _read(self.config.agent_root / ".yy" / "agents" / "AGENT.md")
        sections = [
            skill_xml,
            "# Agent 身份（SOUL）\n" + soul,
            (
                "# Skill 使用策略\n"
                "处理每个用户任务前，必须先检查最上方 <available_skills> 目录。"
                "只要任务与某个 Skill 的 name 或 description 明确匹配，就应优先调用 "
                "skill_read 读取该 Skill 的 SKILL.md，并按照其中的工作流执行；"
                "不要在尚未读取匹配 Skill 时自行改用通用工具流程。"
                "如果需要 Skill 引用的其他文本资源，再使用 skill_read 按需读取，避免一次加载无关内容。"
                "只有不存在匹配 Skill、Skill 读取失败，或用户明确要求不使用 Skill 时，才直接采用通用工具。"
            ),
            (
                "# 核心规则\n你是严谨、透明的本地 Agent。工具调用必须遵守权限、工作区边界和审批要求。"
                "网络调研中，web_search 只用于发现候选 URL；需要读取正文或核验摘要时，"
                "应从搜索结果选择相关 URL 继续调用 web_fetch。需要保存公开论文 PDF 时调用 "
                "download_paper，成功后使用其返回路径调用 read_file；不要把 HTML 页面当作 PDF 下载。"
            ),
            "# 项目说明（AGENT）\n" + agent,
            (
                "# 运行时上下文规则\n"
                "当前时间、工作区、Session、Sandbox、长期记忆和压缩摘要只会出现在当前 "
                "user query 末尾的 ephemeral agent_runtime_context 中。该区块不属于用户原文，"
                "不得整块复制到回答、Memory、文件、日志或 Tool 参数中。"
            ),
        ]
        snapshot = SystemPromptSnapshot(
            session_id=session_id,
            segment_path=segment_path,
            initialized_at=initialized_at,
            content="\n\n".join(sections),
            skill_catalog=selected_catalog,
        )
        self._snapshots[session_id] = snapshot
        self.rebuild_count += 1
        if self.skills is not None and selected_catalog is not None:
            self.skills.bind_session(session_id, selected_catalog)
        return snapshot

    def discard(self, session_id: str) -> None:
        self._snapshots.pop(session_id, None)
        if self.skills is not None:
            self.skills.unbind_session(session_id)

    def discard_all(self) -> None:
        """在长期 Profile 更新后使所有 Session 快照失效。"""
        for session_id in tuple(self._snapshots):
            self.discard(session_id)

    def skill_catalog(self, session_id: str) -> SkillCatalogSnapshot | None:
        snapshot = self.open_session(session_id)
        return snapshot.skill_catalog


class TaskPromptComposer:
    """当前用户消息在 Provider 投影之前保持原文。"""

    def compose(self, task: str) -> dict[str, str]:
        return {"role": "user", "content": task}


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
        self.dynamic_context = AgentDynamicContextBuilder(config, memory)

    def compose(self, task: str, session_id: str | None = None) -> list[dict[str, str]]:
        if session_id is None:
            # 独立使用时没有会话索引，只返回缓存前的系统来源与任务消息。
            skill_xml = self.system.skills.catalog_xml() if self.system.skills is not None else "<available_skills></available_skills>"
            content = "\n\n".join([skill_xml, "# Agent 身份（SOUL）\n（独立 Prompt 模式）"])
            return [{"role": "system", "content": content}, self.task.compose(task)]
        snapshot = self.system.open_session(session_id)
        return [{"role": "system", "content": snapshot.content}, self.task.compose(task)]

    def refresh(
        self,
        session_id: str,
        *,
        skill_catalog: SkillCatalogSnapshot | None = None,
    ) -> SystemPromptSnapshot:
        if skill_catalog is None:
            existing = self.system._snapshots.get(session_id)
            if existing is not None:
                skill_catalog = existing.skill_catalog
        return self.system.open_session(
            session_id,
            force=True,
            skill_catalog=skill_catalog,
        )

    def skill_catalog(self, session_id: str) -> SkillCatalogSnapshot | None:
        return self.system.skill_catalog(session_id)

    def close(self, session_id: str) -> None:
        self.system.discard(session_id)

    def invalidate_all(self) -> None:
        self.system.discard_all()

    def set_sandbox_status(self, status: "SandboxStatus") -> None:
        self.system.set_sandbox_status(status)
        self.dynamic_context.set_sandbox_mode(status.mode)

    def render_provider_query(
        self,
        original_query: str,
        session_id: str,
        *,
        origin_refs: dict[str, str] | None = None,
    ) -> str:
        return self.dynamic_context.render(
            original_query,
            session_id,
            origin_refs=origin_refs,
        )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else "（未配置）"
