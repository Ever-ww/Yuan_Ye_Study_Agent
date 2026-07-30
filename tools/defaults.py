"""项目默认启用的工具集合。"""

from pathlib import Path
from typing import TYPE_CHECKING

from .bash import BashTool
from .calculator import CalculatorTool
from .current_time import CurrentTimeTool
from .edit import EditTool
from .read_file import ReadFileTool
from .registry import AsyncToolRegistry
from .sandbox_rollback import SandboxRollbackTool
from .search_workspace import SearchWorkspaceTool
from .skill_install import SkillInstallTool
from .skill_read import SkillReadTool
from .subagent import SubagentRunner, SubagentTool
from .write import WriteTool

if TYPE_CHECKING:
    from skill import SkillService


def register_subagent(registry: AsyncToolRegistry, runner: SubagentRunner) -> AsyncToolRegistry:
    """在工具层统一注册运行期 Subagent，并返回同一个 Registry。"""
    risks = {name: registry.risk_of(name) for name in registry.names()}
    registry.register(SubagentTool(runner, risks))
    return registry


def default_tools(
    project_root: Path,
    *,
    subagent_runner: SubagentRunner | None = None,
    skill_service: "SkillService | None" = None,
) -> AsyncToolRegistry:
    """装配首期默认工具；项目根目录由执行上下文统一传入。"""
    del project_root  # 保留正式构造接口，工具执行时以 ToolContext 为安全边界。
    builtins = [
        ReadFileTool(),
        EditTool(),
        WriteTool(),
        BashTool(),
        SandboxRollbackTool(),
        CalculatorTool(),
        SearchWorkspaceTool(),
        CurrentTimeTool(),
    ]
    if skill_service is not None:
        builtins.extend([SkillReadTool(skill_service), SkillInstallTool(skill_service)])
    registry = AsyncToolRegistry(builtins)
    if subagent_runner is not None:
        register_subagent(registry, subagent_runner)
    return registry
