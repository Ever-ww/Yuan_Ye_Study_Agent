"""恢复本地沙箱 checkpoint 的高风险工具。"""

from typing import Any

from tool.contracts import ToolContext


class SandboxRollbackTool:
    """分叉式恢复本地工作区；原分支后续内容保持可恢复。"""

    name = "sandbox_rollback"
    description = "把真实工作目录精确恢复到本地 checkpoint，并把原后续内容保存在归档分支"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "steps": {"type": "integer", "minimum": 1},
            "sequence": {"type": "integer", "minimum": 1},
            "checkpoint_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            "merge_eligible": {"type": "boolean", "default": True},
            "archive_reason": {"type": "string", "default": "user_rollback"},
        },
        "oneOf": [
            {"required": ["steps"]},
            {"required": ["sequence"]},
            {"required": ["checkpoint_sha"]},
        ],
    }
    risk = "high"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 checkpoint，无法回溯")
        result = await context.sandbox.rollback(
            arguments.get("steps"),
            sequence=arguments.get("sequence"),
            checkpoint_sha=arguments.get("checkpoint_sha"),
            merge_eligible=arguments.get("merge_eligible", True),
            archive_reason=arguments.get("archive_reason", "user_rollback"),
        )
        return (
            f"已恢复 checkpoint {result.restored.commit_sha}；"
            f"后续 {len(result.preserved_future)} 个 checkpoint 已保存在归档分支 "
            f"{result.archived_branch.branch_id}；新活动分支为 {result.new_active_branch.branch_id}"
        )
