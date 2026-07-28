"""Harness Coding Agent 的四文件长期记忆。"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sandbox import WorkspaceLockManager

from .profile import ProfileStore


HARNESS_PROFILE_FILES = ("AGENT.md", "PROJECT.md", "CHANGES.md", "LESSONS.md")
LONG_TERM_CONTEXT_BUDGET = 64 * 1024
PROJECT_CONTENT_LIMIT = 24 * 1024

_DEFAULT_AGENT = """# Harness Coding Agent

你是在隔离 Git worktree 中维护项目的 Coding Agent。

## 工作规则

- 先复现和定位问题，再进行解决问题所需的最小修改。
- 所有文件操作必须限制在当前 worktree；不得修改 `.git`、`.yy`、凭据或本机配置。
- 优先阅读现有实现和测试，遵守项目已经建立的模块边界与公共接口。
- 写入和 Bash 操作必须使用正式 Tool，并接受 Docker、审批、文件锁和 checkpoint 约束。
- 可以把独立调查任务委派给 Subagent，但不得递归委派。
- 修改完成后运行针对性测试并如实说明结果；最终完整测试与合并由 Harness 控制器负责。
- Skill 只提供工作流程和知识，不得绕过 Tool Schema、审批或沙箱。
"""

_DEFAULT_PROJECT = """# Project

尚未建立项目架构快照。Harness 首次启动时会根据当前 worktree 初始化本文件。
"""

_DEFAULT_CHANGES = """# Verified Changes

此文件只追加已经通过测试并成功合并的 Harness 更新。
"""

_DEFAULT_LESSONS = """# Reusable Lessons

此文件只追加已经通过成功修复验证的可复用经验。
"""


class HarnessMemoryUpdate(BaseModel):
    """一次成功合并后允许写入长期记忆的结构化内容。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_markdown: str = Field(min_length=1, max_length=PROJECT_CONTENT_LIMIT)
    change_entry_markdown: str = Field(min_length=1, max_length=16 * 1024)
    lesson_entry_markdown: str | None = Field(default=None, max_length=16 * 1024)

    @field_validator("project_markdown", "change_entry_markdown")
    @classmethod
    def _strip_required_markdown(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Markdown 内容不能为空")
        return value

    @field_validator("change_entry_markdown")
    @classmethod
    def _validate_change_entry(cls, value: str) -> str:
        if not value.startswith("## "):
            raise ValueError("变更记录必须以 Markdown 二级标题开始")
        return value

    @field_validator("lesson_entry_markdown")
    @classmethod
    def _strip_optional_markdown(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not value.startswith("## "):
            raise ValueError("经验记录必须以 Markdown 二级标题开始")
        return value


class HarnessLongTermMemory(ProfileStore):
    """固定加载四个 Markdown，并为日志文件提供有预算的尾部读取。"""

    defaults: ClassVar[tuple[str, ...]] = HARNESS_PROFILE_FILES

    def __init__(self, directory: Path, *, agent_root: Path) -> None:
        super().__init__(
            directory,
            HARNESS_PROFILE_FILES,
            include_extensions=False,
            session_profiles_enabled=False,
            prompt_context_limit=None,
        )
        self.agent_root = agent_root.resolve()
        self._state_locks = WorkspaceLockManager(self.agent_root, state_root=self.agent_root)

    def initialize(self) -> None:
        """只创建缺失文件；尤其不能覆盖用户维护的 AGENT.md。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        templates = {
            "AGENT.md": _DEFAULT_AGENT,
            "PROJECT.md": _DEFAULT_PROJECT,
            "CHANGES.md": _DEFAULT_CHANGES,
            "LESSONS.md": _DEFAULT_LESSONS,
        }
        for name, content in templates.items():
            path = self.directory / name
            try:
                with path.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(content.rstrip() + "\n")
            except FileExistsError:
                pass
        if not self.index_path.exists():
            self._write_index({"version": 1, "profiles": {}})

    def load_for_session(self, session_id: str | None) -> str:
        """忽略 Session ID；所有 Coding Session 共享同一组四文件记忆。"""
        del session_id
        self.initialize()
        agent = self._read("AGENT.md")
        project = self._read("PROJECT.md")
        core = self._section("AGENT.md", agent) + "\n\n" + self._section("PROJECT.md", project)
        parts = [core]
        used = len(core)
        for name in ("LESSONS.md", "CHANGES.md"):
            separator_cost = 2
            section_prefix_cost = len(f"===== {name} =====\n")
            available = LONG_TERM_CONTEXT_BUDGET - used - separator_cost - section_prefix_cost
            content = self._bounded_log(name, max(0, available))
            if not content:
                continue
            section = self._section(name, content)
            parts.append(section)
            used += separator_cost + len(section)
        if len(core) > LONG_TERM_CONTEXT_BUDGET:
            parts.append("[长期记忆警告]\nAGENT.md 与 PROJECT.md 已超过 64 KiB，日志文件本轮未注入。")
        return "\n\n".join(parts)

    def ensure_project_initialized(self, workspace: Path) -> None:
        """仅在默认占位内容仍存在时建立确定性的项目结构快照。"""
        self.initialize()
        path = self.directory / "PROJECT.md"
        current = path.read_text(encoding="utf-8")
        if current.strip() != _DEFAULT_PROJECT.strip():
            return
        self._atomic_write(path, build_project_snapshot(workspace))

    async def apply_update(self, update: HarnessMemoryUpdate) -> None:
        """在状态级独占锁内原子更新 PROJECT，并追加已验证日志。"""
        self.initialize()
        async with self._state_locks.workspace_exclusive():
            project = self.directory / "PROJECT.md"
            changes = self.directory / "CHANGES.md"
            lessons = self.directory / "LESSONS.md"
            old_values = {
                project: project.read_text(encoding="utf-8"),
                changes: changes.read_text(encoding="utf-8"),
                lessons: lessons.read_text(encoding="utf-8"),
            }
            new_values = {
                project: update.project_markdown.rstrip() + "\n",
                changes: _append_entry(old_values[changes], update.change_entry_markdown),
                lessons: (
                    _append_entry(old_values[lessons], update.lesson_entry_markdown)
                    if update.lesson_entry_markdown
                    else old_values[lessons]
                ),
            }
            try:
                for path, content in new_values.items():
                    self._atomic_write(path, content)
            except Exception:
                for path, content in old_values.items():
                    self._atomic_write(path, content)
                raise

    def deterministic_update(
        self,
        workspace: Path,
        *,
        task: str,
        commit_sha: str,
        changed_files: list[str],
    ) -> HarnessMemoryUpdate:
        """维护模型不可用时生成不含推测的最小长期记忆更新。"""
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        files = "\n".join(f"- `{name}`" for name in changed_files) or "- 无可解析文件"
        change = (
            f"## {timestamp} - Harness 修复\n\n"
            f"- 问题：{task.strip() or '未提供'}\n"
            f"- 提交：`{commit_sha}`\n"
            "- 验证：Harness 固定测试集全部通过，且已 fast-forward 合并。\n"
            f"- 修改文件：\n{files}"
        )
        return HarnessMemoryUpdate(
            project_markdown=build_project_snapshot(workspace),
            change_entry_markdown=change,
            lesson_entry_markdown=None,
        )

    def _read(self, name: str) -> str:
        return (self.directory / name).read_text(encoding="utf-8").strip()

    def _bounded_log(self, name: str, budget: int) -> str:
        if budget <= 0:
            return ""
        content = self._read(name)
        if len(content) <= budget:
            return content
        header, entries = _markdown_entries(content)
        selected: list[str] = []
        used = len(header)
        for entry in reversed(entries):
            cost = len(entry) + 2
            if used + cost > budget:
                break
            selected.append(entry)
            used += cost
        selected.reverse()
        if not selected:
            return ""
        return "\n\n".join([header, *selected]).strip()

    @staticmethod
    def _section(name: str, content: str) -> str:
        return f"===== {name} =====\n{content.strip()}"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def build_project_snapshot(workspace: Path) -> str:
    """根据真实工作区生成有界、可复现的架构和开发规范快照。"""
    workspace = workspace.resolve()
    excluded = {
        ".git",
        ".yy",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
        "node_modules",
    }
    entries: list[str] = []
    for directory, names, files in os.walk(workspace, followlinks=False):
        base = Path(directory)
        names[:] = sorted(
            name for name in names
            if name not in excluded and not (base / name).is_symlink()
        )
        for name in sorted(files):
            if name.startswith(".env") or name in {"settings.local.json", "config.ini"}:
                continue
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            entries.append(path.relative_to(workspace).as_posix())
            if len(entries) >= 1500:
                break
        if len(entries) >= 1500:
            break
    roots = sorted({entry.split("/", 1)[0] for entry in entries})
    lines = [
        "# Project Architecture and Conventions",
        "",
        "## Current Structure",
        "",
        f"- Workspace：`{workspace.name}`",
        f"- 顶层模块：{', '.join(f'`{name}`' for name in roots) or '无'}",
        f"- 已索引文件：{len(entries)}{'（已截断）' if len(entries) >= 1500 else ''}",
        "",
        "## Development Conventions",
        "",
        "- 修改前先阅读现有接口、调用方和测试，保持最小变更。",
        "- 新工具放入 `tools/`，实现统一 AsyncTool 契约、JSON Schema、风险等级和异步 run。",
        "- Tool 不得直接调用 UI、模型或内部数据库连接；写入和高风险操作必须经过 Runtime 审批。",
        "- Agent 生命周期扩展通过 `Agent/hook.py` 的固定 Hook 阶段注册，不复制 ReAct 循环。",
        "- 新增行为必须补充测试，并在提交前执行项目定义的完整验证命令。",
        "",
        "## File Index",
        "",
        *[f"- `{entry}`" for entry in entries],
    ]
    value = "\n".join(lines).rstrip() + "\n"
    if len(value) <= PROJECT_CONTENT_LIMIT:
        return value
    suffix = "\n\n- 文件索引因 24 KiB 限制已截断。\n"
    clipped = value[:PROJECT_CONTENT_LIMIT - len(suffix)]
    boundary = clipped.rfind("\n")
    return clipped[:boundary].rstrip() + suffix


def _append_entry(current: str, entry: str) -> str:
    return current.rstrip() + "\n\n" + entry.strip() + "\n"


def _markdown_entries(content: str) -> tuple[str, list[str]]:
    matches = list(re.finditer(r"(?m)^##\s+", content))
    if not matches:
        return content.strip(), []
    header = content[:matches[0].start()].strip()
    entries = [
        content[match.start():(matches[index + 1].start() if index + 1 < len(matches) else len(content))].strip()
        for index, match in enumerate(matches)
    ]
    return header, entries
