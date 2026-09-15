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
    from sandbox import PathMappingSnapshot
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
        resource_agent_root: Path | None = None,
        prefer_current_skill_catalog: bool = False,
    ) -> None:
        self.config = config
        self.memory = memory
        self.skills = skills
        self.sandbox_mode = "pending" if sandbox_enabled else "closed"
        self.resource_agent_root = (resource_agent_root or config.agent_root).resolve()
        self.prefer_current_skill_catalog = prefer_current_skill_catalog
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
        if (
            selected_catalog is None
            and self.prefer_current_skill_catalog
            and self.skills is not None
        ):
            selected_catalog = self.skills.catalog_snapshot()
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
        soul = _read(self.resource_agent_root / ".yy" / "agents" / "SOUL.md")
        agent = _read(self.resource_agent_root / ".yy" / "agents" / "AGENT.md")
        sections = [
            skill_xml,
            (
                "# Logical filesystem paths\n"
                "Use the workspace path published in runtime context. Never infer or depend on a host absolute path. "
                "YYWorkspace:\\ on Windows and /yy/workspace on POSIX name the current task workspace; "
                "a successful logical mapping does not grant permission, so every Tool and Sandbox policy still applies."
            ),
            (
                "# 能力缺口与 Tool 演进\n"
                "在因为缺少 Tool 能力而告诉用户无法完成之前，先检查当前 Tool Schema，判断已有 Tool 或已有 Tool 的安全组合"
                "是否能够完成。若确实不存在所需能力且当前提供了 `harness_capability`，必须调用 `harness_capability`，不要只"
                "报告能力不足。提交一个具体、可复用的 Tool 能力缺口，准确填写 summary、desired_behavior、"
                "current_limitation、可验证的 acceptance_criteria 以及适用的 safety_constraints。该调用只会启动受控的"
                "Coding/Harness 审批、隔离 worktree、验证、合并和 reload 流程；流程成功前不得声称新 Tool 已经存在。"
                "普通工作区代码任务若能用 read/write/edit/bash 完成，不得制造新 Tool；参数错误、缺少凭据、Policy 或审批"
                "拒绝、临时 Provider/网络/Tool 故障，以及试图绕过既有安全边界，也都不是能力演进。若当前没有"
                " `harness_capability`，才说明限制以及精确缺失的能力。"
            ),
            (
                "# Tool concurrency\n"
                "相互独立的只读工具可以在同一模型响应中一起调用；存在数据依赖时必须等待前一个结果。"
                "不要并行规划写入、高风险或有副作用的工具。"
            ),
            "# Agent 身份（SOUL）\n" + soul,
            (
                "# Skill 使用策略\n"
                "先在不调用工具的情况下判断当前任务是否与最上方 <available_skills> 目录明确匹配。"
                "只要任务与某个 Skill 的 name 或 description 明确匹配，就应优先调用 "
                "skill_read 读取该 Skill 的 SKILL.md，并按照其中的工作流执行；"
                "不要在尚未读取匹配 Skill 时自行改用通用工具流程。"
                "如果需要 Skill 引用的其他文本资源，再使用 skill_read 按需读取，避免一次加载无关内容。"
                "只有不存在匹配 Skill、Skill 读取失败，或用户明确要求不使用 Skill 时，才直接采用通用工具。"
            ),
            (
                "# 核心规则\n你是严谨、透明的本地 Agent。工具调用必须遵守权限、工作区边界和审批要求。"
                "寒暄、自我介绍、能力介绍和已有上下文足以回答的简单对话必须直接回答；"
                "不得为了个性化、自我介绍或确认自身能力而读取 Profile、Skill、工作区或调用其他工具。"
                "只有回答当前请求确实依赖外部事实、文件、计算或操作时才调用工具。"
                "网络调研中，web_search 只用于发现候选 URL；需要读取正文或核验摘要时，"
                "应从搜索结果选择相关 URL 继续调用 web_fetch。需要保存公开论文 PDF 时调用 "
                "download_paper，成功后使用其返回路径调用 read_file；不要把 HTML 页面当作 PDF 下载。"
            ),
            "# 项目说明（AGENT）\n" + agent,
            (
                "# 运行时上下文规则\n"
                "时间、工作区、Session、Sandbox、相关记忆和压缩摘要以增量区块附在 user query 后。"
                "同名区块以最近一次更新为准；active=false 表示撤回。没有新更新时沿用历史区块。"
                "这些区块保留在请求历史中，独立于用户原文；ephemeral 标记表示不属于对话正文，"
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

    def __init__(self, config: "RuntimeConfig | Path", memory: "MemoryStore | SkillService | None" = None, skills: "SkillService | None" = None, sandbox_enabled: bool = False, *, resource_agent_root: Path | None = None, prefer_current_skill_catalog: bool = False, path_mapping: "PathMappingSnapshot | None" = None) -> None:
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
        self.system = SystemPromptComposer(
            config, memory, skills, sandbox_enabled=sandbox_enabled,
            resource_agent_root=resource_agent_root,
            prefer_current_skill_catalog=prefer_current_skill_catalog,
        )
        self.task = TaskPromptComposer()
        self.dynamic_context = AgentDynamicContextBuilder(config, memory, path_mapping=path_mapping)

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
        self.dynamic_context.sandbox_shell = status.shell or ("bash" if status.mode == "docker" else None)

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

    def preview_provider_query(
        self,
        original_query: str,
        session_id: str,
        *,
        origin_refs: dict[str, str] | None = None,
    ) -> str:
        """Render a token-budget preview without changing injection metrics or persistence."""
        return self.dynamic_context.render(
            original_query,
            session_id,
            origin_refs=origin_refs,
            track=False,
        )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else "（未配置）"
