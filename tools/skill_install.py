"""经审批获取、审核并事务安装 Skill 的高风险工具。"""

from __future__ import annotations

from typing import Any

from skill import SkillInstallRequest, SkillService

from .contracts import ToolContext


class SkillInstallTool:
    """自然语言安装入口；Registry 负责首次下载意图审批。"""

    name = "skill_install"
    description = "从公开 GitHub 或当前 workspace 本地目录审核并安装/更新 Skill"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "ref": {"type": "string"},
            "skill_path": {"type": "string"},
            "action": {"type": "string", "enum": ["install", "update"]},
            "name": {"type": "string"},
        },
        "required": ["source"],
    }
    risk = "high"

    def __init__(self, service: SkillService) -> None:
        self.service = service

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        request = SkillInstallRequest(
            source=arguments["source"],
            ref=arguments.get("ref"),
            skill_path=arguments.get("skill_path"),
            action=arguments.get("action") or "install",
            name=arguments.get("name"),
        )
        result = await self.service.install(request)
        return result.model_dump_json(exclude_none=True)
