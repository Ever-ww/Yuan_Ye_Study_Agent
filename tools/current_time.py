"""本地时间查询工具。"""

from datetime import datetime
from typing import Any

from tool.contracts import ToolContext


class CurrentTimeTool:
    extension_preapproval = True
    """返回运行主机带时区的当前本地时间。"""

    name = "current_time"
    description = "获取当前本地时间"
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    risk = "read"
    parallel_safe = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")
