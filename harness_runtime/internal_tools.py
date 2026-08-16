"""Small trigger-bound Harness helpers; execution authority remains in the Engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tool.contracts import ToolContext

from .context import current_harness_trace
from .models import HarnessRuntimeTrigger, canonical_json


class HarnessPreflightTool:
    risk = "read"
    idempotency = "PURE"
    delegatable = False
    runtime_profiles = ("harness",)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, trigger: HarnessRuntimeTrigger) -> None:
        self.trigger = trigger
        self.name = f"harness_{trigger.value}_preflight"
        self.description = (
            "Check the current Harness trace identity and isolated worktree before editing; "
            "this does not commit, merge, or replace authoritative validation"
        )

    def is_available(self, context: ToolContext) -> bool:
        trace = current_harness_trace()
        return bool(trace is not None and trace.trigger is self.trigger)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del arguments
        trace = current_harness_trace()
        if trace is None or trace.trigger is not self.trigger:
            raise PermissionError("Harness trigger context does not authorize this tool")
        root = context.project_root.resolve()
        if not root.is_dir() or not (root / ".git").exists():
            # Git worktrees normally contain a .git pointer file.
            raise RuntimeError("Harness worktree identity is unavailable")
        return canonical_json({
            "status": "ready",
            "trigger": trace.trigger.value,
            "target": trace.target,
            "invocation_id": trace.invocation_id,
            "workspace_name": Path(root).name,
            "authoritative_validation_required": True,
        })
