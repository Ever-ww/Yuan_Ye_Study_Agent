"""受控的工具注册和调度。"""

from __future__ import annotations

from typing import Any

from tools import Tool


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具已注册：{tool.name}")
        self._tools[tool.name] = tool

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"工具不存在：{name}。可用工具：{', '.join(self._tools)}"
        try:
            return tool.run(arguments)
        except (KeyError, TypeError, ValueError) as exc:
            return f"工具 {name} 执行失败：{exc}"

    def prompt_schema(self) -> list[dict[str, Any]]:
        return [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in self._tools.values()
        ]
