"""项目默认启用的工具集合。"""

from pathlib import Path

from .calculator import CalculatorTool
from .current_time import CurrentTimeTool
from .read_file import ReadFileTool
from .registry import AsyncToolRegistry
from .search_workspace import SearchWorkspaceTool
from .write_file import WriteFileTool


def default_tools(project_root: Path) -> AsyncToolRegistry:
    """装配首期默认工具；项目根目录由执行上下文统一传入。"""
    del project_root  # 保留正式构造接口，工具执行时以 ToolContext 为安全边界。
    return AsyncToolRegistry([
        ReadFileTool(),
        WriteFileTool(),
        CalculatorTool(),
        SearchWorkspaceTool(),
        CurrentTimeTool(),
    ])
