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
        self._loaded: set[tuple[str, str, str, str]] = set()
        self.cache_hits = 0
        self.cache_misses = 0

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not context.session_id:
            raise RuntimeError("skill_read 需要绑定到活动 Session")
        snapshot = self.service.session_snapshot(context.session_id)
        name = str(arguments["name"])
        path = str(arguments.get("path") or "SKILL.md")
        key = (context.session_id, name, path, snapshot.digest)
        if key in self._loaded:
            self.cache_hits += 1
            entry = snapshot.by_name().get(name)
            if entry is None:
                raise KeyError(f"当前 Session 未启用 Skill：{name}")
            return f"skill-ref:{name}:{path}:{entry.content_digest}"
        content = await asyncio.to_thread(
            self.service.read,
            snapshot,
            name,
            path,
        )
        self._loaded.add(key)
        self.cache_misses += 1
        return content
