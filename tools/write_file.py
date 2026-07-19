"""需要审批的工作区原子写入工具。"""

from typing import Any
from uuid import uuid4

from .contracts import ToolContext
from .path_guard import safe_workspace_path


class WriteFileTool:
    """经 Runtime 批准后，原子写入工作区内的 UTF-8 文本。"""

    name = "write_file"
    description = "写入工作区文本文件"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    risk = "write"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = safe_workspace_path(context.project_root, arguments["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(arguments["content"], encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return f"已写入 {arguments['path']}"
