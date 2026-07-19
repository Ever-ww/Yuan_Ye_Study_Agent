"""异步工具扩展协议与最小执行上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from Agent.contracts import ApprovalCallback


class AsyncTool(Protocol):
    """所有工具实现都必须遵守的稳定协议。"""

    name: str
    description: str
    schema: dict[str, Any]
    risk: str

    async def run(self, arguments: dict[str, Any], context: "ToolContext") -> str: ...


@dataclass(frozen=True)
class ToolContext:
    """工具执行时可用的最小受控上下文。"""

    project_root: Path
    approval: ApprovalCallback | None = None
