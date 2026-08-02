"""已审核 Skill 的只读渐进加载工具。"""

from __future__ import annotations

import asyncio
from typing import Any

from skill import SkillService

from tool.contracts import ToolContext


class SkillReadTool:
    """只读取已安装且内容摘要仍匹配的 Skill 文本资源。"""

    name = "skill_read"
    description = "读取已审核 Skill 的 SKILL.md、references 或脚本文本；不会执行脚本"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["name"],
    }
    risk = "read"

    def __init__(self, service: SkillService) -> None:
        self.service = service

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        return await asyncio.to_thread(
            self.service.read,
            arguments["name"],
            arguments.get("path") or "SKILL.md",
        )
