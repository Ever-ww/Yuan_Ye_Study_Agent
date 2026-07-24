"""异步工具扩展协议与最小执行上下文。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict


ToolRisk = Literal["read", "write", "high", "dynamic"]

class AsyncTool(Protocol):
    """所有工具实现都必须遵守的稳定协议。"""

    name: str
    description: str
    schema: dict[str, Any]
    risk: ToolRisk

    async def run(self, arguments: dict[str, Any], context: "ToolContext") -> str: ...


class ToolContext(BaseModel):
    """工具执行时可用的最小受控上下文。"""

    model_config = ConfigDict(frozen=True, strict=True, arbitrary_types_allowed=True)

    project_root: Path
    approval: Callable[[str, dict[str, Any]], Awaitable[bool]] | None = None
