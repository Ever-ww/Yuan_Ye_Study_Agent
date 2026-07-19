"""受控工作区文本读取工具。"""

from typing import Any

from .contracts import ToolContext
from .path_guard import safe_workspace_path


class ReadFileTool:
    """读取工作区内的 UTF-8 文本，并限制单次返回长度。"""

    name = "read_file"
    description = "读取工作区文本文件"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    risk = "read"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = safe_workspace_path(context.project_root, arguments["path"])
        return path.read_text(encoding="utf-8")[:20000]
