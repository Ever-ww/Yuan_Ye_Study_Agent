"""需要审批的工作区整文件原子写入工具。"""

from typing import Any
from uuid import uuid4

from .contracts import ToolContext
from .path_guard import safe_workspace_path


class WriteTool:
    """经 Runtime 批准后，原子写入工作区内的 UTF-8 文本。"""

    name = "write"
    description = "创建工作区文本文件或完整替换已有文件"
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
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 checkpoint，禁止执行 write")
        if context.file_locks is None:
            raise RuntimeError("当前 Runtime 未启用文件锁，禁止执行 write")
        path = safe_workspace_path(context.project_root, arguments["path"])
        content = arguments["content"]
        async with context.file_locks.write(path):
            if path.is_file() and path.read_bytes() == content.encode("utf-8"):
                return f"文件内容未变化：{arguments['path']}；未创建 checkpoint"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(content, encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            try:
                checkpoint = await context.sandbox.checkpoint_write(arguments["path"])
            except Exception:
                await context.sandbox.restore_current()
                raise
            if checkpoint is None:
                return f"已写入 {arguments['path']}；工作区无可提交变化"
            return f"已写入 {arguments['path']}；checkpoint {checkpoint.commit_sha}"
