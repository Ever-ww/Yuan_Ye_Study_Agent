"""只允许在 Trace 级 Docker 沙箱中执行的 Bash 工具。"""

from typing import Any

from .contracts import ToolContext


class BashTool:
    """执行受限 Bash，并由沙箱在实际修改后创建一个 checkpoint。"""

    name = "bash"
    description = "在无网络 Docker 沙箱中执行 Bash 命令；项目文件修改会同步到工作目录"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
        },
        "required": ["command"],
    }
    risk = "high"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 Docker 沙箱，禁止执行 Bash")
        timeout = arguments.get("timeout_seconds")
        result = await context.sandbox.run_bash(
            arguments["command"],
            30 if timeout is None else timeout,
        )
        checkpoint = (
            f"\ncheckpoint: {result.checkpoint.commit_sha}"
            if result.checkpoint is not None
            else "\ncheckpoint: 无文件变化"
        )
        return (result.output or "命令执行成功，无输出") + checkpoint
