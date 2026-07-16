"""统一导出旧版同步工具与新版异步 Harness 工具。

异步 Harness 模块依赖 Agent 运行时，如果在包初始化时直接导入，
容易与 ``Agent`` 的兼容导出形成循环依赖。因此新 API 通过
模块级 ``__getattr__`` 按需加载，旧 API 则保持直接导入的行为。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .base import Tool
from .calculator import CalculatorTool
from .current_time import CurrentTimeTool

_HARNESS_EXPORTS = {
    "AsyncToolRegistry": (".harness", "ToolRegistry"),
    "BaseTool": (".harness", "BaseTool"),
    "PathPolicy": (".harness", "PathPolicy"),
    "ToolContext": (".harness", "ToolContext"),
    "default_tools": (".harness", "default_tools"),
}

__all__ = ["CalculatorTool", "CurrentTimeTool", "Tool", *_HARNESS_EXPORTS]


def __getattr__(name: str) -> Any:
    """在首次访问异步工具导出时延迟导入并缓存结果。

    这里仅允许 ``_HARNESS_EXPORTS`` 中的固定名称，不会将任意用户输入
    作为模块路径，因而不会引入动态导入注入问题。
    """

    target = _HARNESS_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    # 写回模块全局空间，后续访问与普通导入一样无额外开销。
    globals()[name] = value
    return value
