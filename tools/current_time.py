"""本地时间查询工具。"""

from datetime import datetime
from typing import Any

from .contracts import ToolContext


class CurrentTimeTool:
    """返回运行主机带时区的当前本地时间。"""

    name = "current_time"
    description = "获取当前本地时间"
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "read"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")
