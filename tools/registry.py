"""异步工具注册、Schema 校验与权限审批。"""

from __future__ import annotations

import re
from typing import Annotated, Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from .contracts import AsyncTool, ToolContext, ToolRisk


_STATIC_RISKS = {"read", "write", "high"}
_ALL_RISKS = {*_STATIC_RISKS, "dynamic"}


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
        if tool.risk not in _ALL_RISKS:
            raise ValueError(f"工具 {tool.name} 的风险等级无效：{tool.risk}")
        if tool.risk == "dynamic" and not callable(getattr(tool, "risk_for", None)):
            raise ValueError(f"动态风险工具 {tool.name} 必须实现 risk_for(arguments)")
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
            if name in {"subagent", "skill_install"}:
                raise ValueError(f"子 Agent 不允许选择工具：{name}")
            tool = self._tools.get(name)
            if tool is None:
                raise ValueError(f"未知工具：{name}")
            if tool not in selected:
                selected.append(tool)
        return AsyncToolRegistry(selected)

    def risk_of(self, name: str, arguments: dict[str, Any] | None = None) -> ToolRisk:
        """返回静态或基于已校验参数计算出的动态风险等级。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未知工具：{name}")
        if arguments is None:
            return tool.risk
        prepared = self.prepare_arguments(name, arguments)
        return self._resolved_risk(tool, self._validate(name, prepared))

    @staticmethod
    def _resolved_risk(tool: AsyncTool, arguments: dict[str, Any]) -> ToolRisk:
        """使用已校验参数解析动态风险，并拒绝工具返回非法等级。"""
        if tool.risk != "dynamic":
            return tool.risk
        risk = tool.risk_for(arguments)
        if risk not in _STATIC_RISKS:
            raise ValueError(f"工具 {tool.name} 计算出了无效风险等级：{risk}")
        return risk

    async def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> str:
        """重新校验 Hook 处理后的参数，获批后执行工具。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未知工具：{name}")
        arguments = self.prepare_arguments(name, arguments)
        arguments = self._validate(name, arguments)
        needs_approval = self._resolved_risk(tool, arguments) != "read"
        if needs_approval:
            if context.approval is None or not await context.approval(name, arguments):
                raise PermissionError(f"工具调用未获批准：{name}")
        return await tool.run(arguments, context)

    def prepare_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """在 Schema 校验前执行工具声明的兼容性规范化。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"未知工具：{name}")
        prepare = getattr(tool, "prepare_arguments", None)
        if not callable(prepare):
            return dict(arguments)
        prepared = prepare(dict(arguments))
        if not isinstance(prepared, dict):
            raise ValueError(f"工具 {name} 的 prepare_arguments 必须返回对象")
        return prepared

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

    model_name = "ToolArguments_" + re.sub(r"\W+", "_", name)
    return _build_object_model(
        model_name,
        properties,
        required,
        tool_name=name,
    )


def _build_object_model(
    model_name: str,
    properties: dict[str, Any],
    required: set[str],
    *,
    tool_name: str,
) -> type[BaseModel]:
    """递归创建严格对象模型，使数组中的对象也在审批前完成校验。"""
    unknown_required = required.difference(properties)
    if unknown_required:
        raise ValueError(
            f"工具 {tool_name} 的 required 包含未知字段：{sorted(unknown_required)[0]}",
        )
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, definition in properties.items():
        if not isinstance(definition, dict):
            raise ValueError(f"工具 {tool_name} 的字段 {field_name} 定义必须是对象")
        safe_field_name = re.sub(r"\W+", "_", field_name)
        nested_name = f"{model_name}_{safe_field_name}"
        annotation = _schema_type(
            definition,
            model_name=nested_name,
            tool_name=tool_name,
        )
        fields[field_name] = (annotation, ... if field_name in required else None)
    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", strict=True),
        **fields,
    )


def _schema_type(
    definition: dict[str, Any],
    *,
    model_name: str,
    tool_name: str,
) -> Any:
    """转换工具参数目前支持的字符串、数组、数值、布尔和对象类型。"""
    kind = definition.get("type")
    if kind == "string":
        allowed = definition.get("enum")
        if isinstance(allowed, list) and allowed:
            return Literal.__getitem__(tuple(allowed))
        return str
    if kind == "array":
        item_definition = definition.get("items", {})
        if not isinstance(item_definition, dict):
            raise ValueError(f"工具 {tool_name} 的数组 items 必须是对象")
        item_type = _schema_type(
            item_definition,
            model_name=f"{model_name}_Item",
            tool_name=tool_name,
        )
        minimum = definition.get("minItems")
        maximum = definition.get("maxItems")
        if isinstance(minimum, int) or isinstance(maximum, int):
            return Annotated[
                list[item_type],
                Field(
                    min_length=minimum if isinstance(minimum, int) else None,
                    max_length=maximum if isinstance(maximum, int) else None,
                ),
            ]
        return list[item_type]
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    if kind == "object":
        properties = definition.get("properties")
        if properties is None:
            return dict[str, Any]
        if not isinstance(properties, dict):
            raise ValueError(f"工具 {tool_name} 的对象 properties 必须是对象")
        required = definition.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError(f"工具 {tool_name} 的对象 required 必须是字符串数组")
        return _build_object_model(
            model_name,
            properties,
            set(required),
            tool_name=tool_name,
        )
    return Any
