"""恢复本地沙箱 checkpoint 的高风险工具。"""

from typing import Any

from .contracts import ToolContext


class SandboxRollbackTool:
    """按 hard-reset 语义回退最近的本地工作区快照。"""

    name = "sandbox_rollback"
    description = "把真实工作目录回退到更早的本地 checkpoint"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"steps": {"type": "integer"}},
        "required": ["steps"],
    }
    risk = "high"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 checkpoint，无法回溯")
        result = await context.sandbox.rollback(arguments["steps"])
        return (
            f"已恢复 checkpoint {result.restored.commit_sha}；"
            f"删除后续 {len(result.removed)} 个 checkpoint"
        )
