"""异步工具注册、Schema 校验与权限审批。"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .contracts import AsyncTool, ToolContext


class AsyncToolRegistry:
    """统一负责工具发现、参数校验和高风险调用审批。"""

    def __init__(self, tools: Iterable[AsyncTool] = ()) -> None:
        self._tools: dict[str, AsyncTool] = {}
        self._argument_models: dict[str, type[BaseModel]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AsyncTool) -> None:
        """注册一个工具，并拒绝名称冲突。"""
        if tool.name in self._tools:
            raise ValueError(f"工具名称重复：{tool.name}")
        self._tools[tool.name] = tool
        self._argument_models[tool.name] = _build_argument_model(tool.name, tool.schema)

    def schemas(self) -> list[dict[str, Any]]:
        """返回供模型调用的 OpenAI function Schema 列表。"""
        return [
            {"name": tool.name, "description": tool.description, "parameters": tool.schema}
            for tool in self._tools.values()
        ]

    def names(self) -> tuple[str, ...]:
        """按注册顺序返回工具名称。"""
        return tuple(self._tools)

    def select(self, names: Iterable[str]) -> "AsyncToolRegistry":
        """创建严格子集；未知名称和 subagent 递归调用会被拒绝。"""
        selected: list[AsyncTool] = []
        for name in names:
            if name == "subagent":
                raise ValueError("子 Agent 不允许递归调用 subagent")
            tool = self._tools.get(name)
            if tool is None:
                raise ValueError(f"未知工具：{name}")
            if tool not in selected:
                selected.append(tool)
        return AsyncToolRegistry(selected)

    def risk_of(self, name: str) -> str:
        """返回指定工具风险等级，供委派审批使用。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未知工具：{name}")
        return tool.risk

    async def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> str:
        """重新校验 Hook 处理后的参数，获批后执行工具。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未知工具：{name}")
        arguments = self._validate(name, arguments)
        dynamic = getattr(tool, "requires_approval", None)
        needs_approval = bool(dynamic(arguments)) if callable(dynamic) else tool.risk != "read"
        if needs_approval:
            if context.approval is None or not await context.approval(name, arguments):
                raise PermissionError(f"工具调用未获批准：{name}")
        return await tool.run(arguments, context)

    def _validate(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """用工具 Schema 对应的 Pydantic 模型严格校验实际执行参数。"""
        model = self._argument_models[name]
        try:
            return model.model_validate(arguments).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise ValueError(f"工具参数校验失败：{exc}") from exc


def _build_argument_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """把当前项目使用的 JSON Schema 子集编译为严格 Pydantic 模型。"""
    if schema.get("type", "object") != "object":
        raise ValueError(f"工具 {name} 的参数 Schema 必须是 object")
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
        raise ValueError(f"工具 {name} 的 properties 必须是对象")
    unknown_required = required.difference(properties)
    if unknown_required:
        raise ValueError(f"工具 {name} 的 required 包含未知字段：{sorted(unknown_required)[0]}")

    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, definition in properties.items():
        if not isinstance(definition, dict):
            raise ValueError(f"工具 {name} 的字段 {field_name} 定义必须是对象")
        annotation = _schema_type(definition)
        fields[field_name] = (annotation, ... if field_name in required else None)
    model_name = "ToolArguments_" + re.sub(r"\W+", "_", name)
    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", strict=True),
        **fields,
    )


def _schema_type(definition: dict[str, Any]) -> Any:
    """转换工具参数目前支持的字符串、数组、数值、布尔和对象类型。"""
    kind = definition.get("type")
    if kind == "string":
        allowed = definition.get("enum")
        if isinstance(allowed, list) and allowed:
            return Literal.__getitem__(tuple(allowed))
        return str
    if kind == "array":
        item_type = _schema_type(definition.get("items", {}))
        return list[item_type]
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    if kind == "object":
        return dict[str, Any]
    return Any
