"""项目默认启用的工具集合。"""

from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import AsyncTool
from .registry import AsyncToolRegistry
from tools.bash import BashTool
from tools.calculator import CalculatorTool
from tools.cronjob import CronJobTool
from tools.current_time import CurrentTimeTool
from tools.download_paper import PaperDownloadTool
from tools.edit import EditTool
from tools.paper_library import (
    PaperLibraryDownloadTool,
    PaperLibraryLookupTool,
    PaperLibraryReadTool,
    PaperLibrarySaveTool,
)
from tools.profile_read import ProfileReadTool
from tools.read_file import ReadFileTool
from tools.reference import ReferenceGetTool, ReferenceSearchTool, ReferenceWriteTool
from tools.sandbox_rollback import SandboxRollbackTool
from tools.search_workspace import SearchWorkspaceTool
from tools.skill_install import SkillInstallTool
from tools.skill_read import SkillReadTool
from tools.subagent import SubagentRunner, SubagentTool
from tools.write import WriteTool

if TYPE_CHECKING:
    from cron import CronService
    from paper_library import PaperLibraryService
    from reference import ReferenceService
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
    paper_download_tool: AsyncTool | None = None,
    cron_service: "CronService | None" = None,
    cron_project_id: str | None = None,
    reference_service: "ReferenceService | None" = None,
    reference_search_mode: str = "rrf",
    agent_root: Path | None = None,
    paper_library_service: "PaperLibraryService | None" = None,
) -> AsyncToolRegistry:
    """装配首期默认工具；项目根目录由执行上下文统一传入。"""
    selected_agent_root = (agent_root or project_root).resolve()
    builtins = [
        ReadFileTool(),
        EditTool(),
        WriteTool(),
        BashTool(),
        SandboxRollbackTool(),
        CalculatorTool(),
        SearchWorkspaceTool(),
        CurrentTimeTool(),
        ProfileReadTool(selected_agent_root),
    ]
    if web_search_tool is not None:
        builtins.append(web_search_tool)
    if web_fetch_tool is not None:
        builtins.append(web_fetch_tool)
    if paper_download_tool is not None:
        builtins.append(paper_download_tool)
    if reference_service is not None:
        builtins.extend([
            ReferenceSearchTool(reference_service, reference_search_mode),
            ReferenceGetTool(reference_service),
            ReferenceWriteTool(reference_service, paper_library_service),
        ])
    if paper_library_service is not None:
        builtins.extend([
            PaperLibraryLookupTool(paper_library_service),
            PaperLibraryDownloadTool(paper_library_service),
            PaperLibraryReadTool(paper_library_service),
            PaperLibrarySaveTool(paper_library_service),
        ])
    if skill_service is not None:
        builtins.extend([SkillReadTool(skill_service), SkillInstallTool(skill_service)])
    if cron_service is not None and cron_project_id is not None:
        builtins.append(CronJobTool(cron_service, cron_project_id))
    registry = AsyncToolRegistry(builtins)
    if subagent_runner is not None:
        register_subagent(registry, subagent_runner)
    return registry
