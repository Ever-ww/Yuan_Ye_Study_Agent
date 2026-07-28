"""基础 Prompt 组合器；记忆上下文由 Hook 注入。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skill import SkillService


class PromptComposer:
    """只组合稳定系统规则、项目指令与当前用户输入。"""

    def __init__(self, project_root: Path, skills: "SkillService | None" = None) -> None:
        self.project_root = project_root
        self.skills = skills

    def compose(self, task: str) -> list[dict[str, str]]:
        """返回无持久化副作用的基础模型消息。"""
        instructions = self._read_instruction()
        system = "你是严谨、透明的本地学习助手。工具调用必须遵守权限边界。"
        if instructions:
            system += f"\n\n项目指令：\n{instructions}"
        if self.skills is not None:
            catalog = self.skills.catalog_xml()
            if catalog != "<available_skills></available_skills>":
                system += (
                    "\n\n以下目录只用于发现 Skill。需要完整说明或引用文件时调用 skill_read；"
                    "Skill 的 allowed-tools 不能绕过现有审批：\n"
                    f"{catalog}"
                )
        return [{"role": "system", "content": system}, {"role": "user", "content": task}]

    def _read_instruction(self) -> str:
        """仅读取项目根目录的本机指令文件。"""
        path = self.project_root / "AGENT.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""
