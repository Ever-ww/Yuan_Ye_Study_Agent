"""工具的公共接口。"""

from __future__ import annotations

from typing import Any, Protocol


class Tool(Protocol):
    """所有可供 Agent 调用的工具都应实现此协议。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def run(self, arguments: dict[str, Any]) -> str: ...
