"""查询分支化 Sandbox Checkpoint 的恢复点和 Dream 状态。"""

from __future__ import annotations

import json
from typing import Any

from tool.contracts import ToolContext


class SandboxCheckpointHistoryTool:
    """只读展示恢复点、分支生命周期和可选的合并尝试。"""

    name = "sandbox_checkpoint_history"
    description = "查看当前会话的 checkpoint 恢复点、归档分支及 Dream 合并状态"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "include_merge_attempts": {"type": "boolean", "default": False},
        },
    }
    risk = "read"
    idempotency = "PURE"
    runtime_profiles = ("interactive",)
    delegatable = False

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 checkpoint，无法查询分支历史")
        payload: dict[str, Any] = {
            "restore_points": [
                record.model_dump(mode="json")
                for record in context.sandbox.list_checkpoints()
            ],
            "branches": [
                branch.model_dump(mode="json")
                for branch in context.sandbox.list_checkpoint_branches()
            ],
        }
        if arguments.get("include_merge_attempts", False):
            payload["merge_attempts"] = [
                attempt.model_dump(mode="json")
                for attempt in context.sandbox.list_checkpoint_merge_attempts()
            ]
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
