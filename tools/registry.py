"""异步工具注册、Schema 校验与权限审批。"""

from __future__ import annotations

from typing import Any, Iterable

from .contracts import AsyncTool, ToolContext


class AsyncToolRegistry:
    """统一负责工具发现、参数校验和高风险调用审批。"""

    def __init__(self, tools: Iterable[AsyncTool] = ()) -> None:
        self._tools: dict[str, AsyncTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AsyncTool) -> None:
        """注册一个工具，并拒绝名称冲突。"""
        if tool.name in self._tools:
            raise ValueError(f"工具名称重复：{tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        """返回供模型调用的 OpenAI function Schema 列表。"""
        return [
            {"name": tool.name, "description": tool.description, "parameters": tool.schema}
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> str:
        """重新校验 Hook 处理后的参数，获批后执行工具。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未知工具：{name}")
        self._validate(tool.schema, arguments)
        if tool.risk != "read":
            if context.approval is None or not await context.approval(name, arguments):
                raise PermissionError(f"工具调用未获批准：{name}")
        return await tool.run(arguments, context)

    @staticmethod
    def _validate(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        """执行当前工具所需的对象、必填字段和基本类型校验。"""
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象")
        for key in schema.get("required", []):
            if key not in arguments:
                raise ValueError(f"缺少工具参数：{key}")
        for key, definition in schema.get("properties", {}).items():
            if key in arguments and definition.get("type") == "string" and not isinstance(arguments[key], str):
                raise ValueError(f"参数 {key} 必须是字符串")
