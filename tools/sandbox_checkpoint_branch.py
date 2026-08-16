"""管理归档 Sandbox Checkpoint 分支 Dream 准入的高风险工具。"""

from __future__ import annotations

from typing import Any

from tool.contracts import ToolContext


class SandboxCheckpointBranchTool:
    """显式启用或禁用归档分支的 Dream 价值评估资格。"""

    name = "sandbox_checkpoint_branch"
    description = "高风险：启用或禁用某个归档 checkpoint 分支参与 Dream 自动合并"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "branch_id": {"type": "string", "minLength": 1},
            "eligible": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "required": ["branch_id", "eligible", "reason"],
        "additionalProperties": False,
    }
    risk = "high"
    idempotency = "IDEMPOTENT"
    runtime_profiles = ("interactive",)
    delegatable = False

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 checkpoint，无法管理归档分支")
        branch = await context.sandbox.set_checkpoint_branch_merge_eligibility(
            arguments["branch_id"],
            arguments["eligible"],
            arguments["reason"],
        )
        return branch.model_dump_json(exclude_none=True)
