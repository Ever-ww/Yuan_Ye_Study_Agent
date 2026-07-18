"""工具协议、注册表及首期内置工具。"""

from __future__ import annotations

import ast
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from Agent.contracts import ApprovalCallback


class AsyncTool(Protocol):
    """工具必须提供名称、Schema、风险等级和异步执行函数。"""

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


class AsyncToolRegistry:
    """集中完成工具发现、参数校验和风险审批。"""

    def __init__(self, tools: list[AsyncTool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def schemas(self) -> list[dict[str, Any]]:
        """返回供模型调用的 OpenAI function Schema 列表。"""
        return [{"name": tool.name, "description": tool.description, "parameters": tool.schema} for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> str:
        """校验输入、请求必要批准后执行工具。"""
        tool = self._tools.get(name)
        if not tool:
            raise ValueError(f"未知工具：{name}")
        self._validate(tool.schema, arguments)
        if tool.risk != "read":
            if not context.approval or not await context.approval(name, arguments):
                raise PermissionError(f"工具调用未获批准：{name}")
        return await tool.run(arguments, context)

    @staticmethod
    def _validate(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        """执行首期所需的对象、必填字段和基本类型校验。"""
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象")
        for key in schema.get("required", []):
            if key not in arguments:
                raise ValueError(f"缺少工具参数：{key}")
        for key, definition in schema.get("properties", {}).items():
            if key in arguments and definition.get("type") == "string" and not isinstance(arguments[key], str):
                raise ValueError(f"参数 {key} 必须是字符串")


@dataclass
class _FunctionTool:
    """用函数快速定义的小型内置工具。"""

    name: str
    description: str
    schema: dict[str, Any]
    risk: str
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[str]]

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """将执行委托给注入的异步处理器。"""
        return await self.handler(arguments, context)


def _safe_path(root: Path, requested: str) -> Path:
    """解析路径并阻止越出项目根目录。"""
    path = (root / requested).resolve()
    if root != path and root not in path.parents:
        raise PermissionError("路径必须位于项目工作区内")
    if path.name.startswith(".env"):
        raise PermissionError("禁止访问敏感配置文件")
    return path


async def _read(arguments: dict[str, Any], context: ToolContext) -> str:
    """读取受限工作区文本文件。"""
    return _safe_path(context.project_root, arguments["path"]).read_text(encoding="utf-8")[:20000]


async def _write(arguments: dict[str, Any], context: ToolContext) -> str:
    """创建父目录后原子语义写入 UTF-8 文本。"""
    path = _safe_path(context.project_root, arguments["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(arguments["content"], encoding="utf-8")
    temporary.replace(path)
    return f"已写入 {arguments['path']}"


async def _calculate(arguments: dict[str, Any], context: ToolContext) -> str:
    """仅计算由数字和四则运算构成的表达式。"""
    tree = ast.parse(arguments["expression"], mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub)
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise ValueError("表达式仅支持数字和四则运算")
    return str(eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {}))


async def _search(arguments: dict[str, Any], context: ToolContext) -> str:
    """在工作区文本文件中执行上限受控的简单字符串检索。"""
    query, matches = arguments["query"].lower(), []
    for path in context.project_root.rglob("*"):
        if len(matches) >= 30 or not path.is_file() or ".yy" in path.parts:
            continue
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if query in line.lower():
                    matches.append(f"{path.relative_to(context.project_root)}:{number}: {line[:200]}")
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(matches) or "未找到匹配内容"


async def _current_time(arguments: dict[str, Any], context: ToolContext) -> str:
    """返回当前本地时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_tools(project_root: Path) -> AsyncToolRegistry:
    """创建首期安全工具集合。"""
    string = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    return AsyncToolRegistry([
        _FunctionTool("read_file", "读取工作区文本文件", string, "read", _read),
        _FunctionTool("write_file", "写入工作区文本文件", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, "write", _write),
        _FunctionTool("calculator", "计算数学表达式", {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}, "read", _calculate),
        _FunctionTool("search_workspace", "搜索工作区文本", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, "read", _search),
        _FunctionTool("current_time", "获取当前本地时间", {"type": "object", "properties": {}}, "read", _current_time),
    ])
