"""仅在 Trace 的 OS 或 Docker 沙箱中执行命令。"""

from typing import Any

from tool.contracts import ToolContext


class BashTool:
    """执行受限 Bash，并由沙箱在实际修改后创建一个 checkpoint。"""

    name = "bash"
    description = "在无网络沙箱执行命令；OS 后端在 Linux/macOS 使用 Bash，Windows 默认 PowerShell（可配置 Git Bash）；Docker 后端使用 Bash。以当前沙箱 shell 状态为准。"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
            "writable_paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 16,
                "description": "Workspace-relative directories this command may modify; omit for whole workspace compatibility.",
            },
        },
        "required": ["command"],
    }
    risk = "high"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用安全沙箱，禁止执行 Bash/Shell")
        timeout = arguments.get("timeout_seconds")
        timeout_seconds = 30 if timeout is None else timeout
        if "writable_paths" in arguments:
            result = await context.sandbox.run_bash(
                arguments["command"], timeout_seconds,
                writable_paths=tuple(arguments["writable_paths"]),
            )
        else:
            # Preserve the small injected Sandbox protocol used by existing
            # runtimes and third-party tests until they opt into path scoping.
            result = await context.sandbox.run_bash(arguments["command"], timeout_seconds)
        checkpoint = (
            f"\ncheckpoint: {result.checkpoint.commit_sha}"
            if result.checkpoint is not None
            else "\ncheckpoint: 无文件变化"
        )
        return (result.output or "命令执行成功，无输出") + checkpoint
