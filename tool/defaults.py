"""项目默认启用的工具集合。"""

from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import AsyncTool
from .registry import AsyncToolRegistry
from tools.bash import BashTool
from tools.calculator import CalculatorTool
from tools.cronjob import CronJobTool
from tools.current_time import CurrentTimeTool
from tools.edit import EditTool
from tools.read_file import ReadFileTool
from tools.sandbox_rollback import SandboxRollbackTool
from tools.search_workspace import SearchWorkspaceTool
from tools.skill_install import SkillInstallTool
from tools.skill_read import SkillReadTool
from tools.subagent import SubagentRunner, SubagentTool
from tools.write import WriteTool

if TYPE_CHECKING:
    from cron import CronService
    from skill import SkillService


def register_subagent(registry: AsyncToolRegistry, runner: SubagentRunner) -> AsyncToolRegistry:
    """在工具层统一注册运行期 Subagent，并返回同一个 Registry。"""
    risks = {name: registry.risk_of(name) for name in registry.names()}
    registry.register(SubagentTool(runner, risks, registry))
    return registry


def default_tools(
    project_root: Path,
    *,
    subagent_runner: SubagentRunner | None = None,
    skill_service: "SkillService | None" = None,
    web_search_tool: AsyncTool | None = None,
    web_fetch_tool: AsyncTool | None = None,
    cron_service: "CronService | None" = None,
    cron_project_id: str | None = None,
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
    if web_search_tool is not None:
        builtins.append(web_search_tool)
    if web_fetch_tool is not None:
        builtins.append(web_fetch_tool)
    if skill_service is not None:
        builtins.extend([SkillReadTool(skill_service), SkillInstallTool(skill_service)])
    if cron_service is not None and cron_project_id is not None:
        builtins.append(CronJobTool(cron_service, cron_project_id))
    registry = AsyncToolRegistry(builtins)
    if subagent_runner is not None:
        register_subagent(registry, subagent_runner)
    return registry
