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
from tools.sandbox_checkpoint_branch import SandboxCheckpointBranchTool
from tools.sandbox_checkpoint_history import SandboxCheckpointHistoryTool
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


def register_subagent(
    registry: AsyncToolRegistry, runner: SubagentRunner, *, tool_module=None,
) -> AsyncToolRegistry:
    """在工具层统一注册运行期 Subagent，并返回同一个 Registry。"""
    risks = {name: registry.risk_of(name) for name in registry.names()}
    tool_type = getattr(tool_module, "SubagentTool", SubagentTool)
    registry.register(tool_type(runner, risks, registry))
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
    runtime_profile: str = "interactive",
    tool_module=None,
    skill_install_service: "SkillService | None" = None,
) -> AsyncToolRegistry:
    """装配首期默认工具；项目根目录由执行上下文统一传入。"""
    selected_agent_root = (agent_root or project_root).resolve()
    def selected(name: str, fallback):
        return getattr(tool_module, name) if tool_module is not None else fallback

    builtins = [
        selected("ReadFileTool", ReadFileTool)(),
        selected("EditTool", EditTool)(),
        selected("WriteTool", WriteTool)(),
        selected("BashTool", BashTool)(),
        selected("SandboxRollbackTool", SandboxRollbackTool)(),
        selected("SandboxCheckpointHistoryTool", SandboxCheckpointHistoryTool)(),
        selected("SandboxCheckpointBranchTool", SandboxCheckpointBranchTool)(),
        selected("CalculatorTool", CalculatorTool)(),
        selected("SearchWorkspaceTool", SearchWorkspaceTool)(),
        selected("CurrentTimeTool", CurrentTimeTool)(),
        selected("ProfileReadTool", ProfileReadTool)(selected_agent_root),
    ]
    if web_search_tool is not None:
        builtins.append(web_search_tool)
    if web_fetch_tool is not None:
        builtins.append(web_fetch_tool)
    if paper_download_tool is not None:
        builtins.append(paper_download_tool)
    if reference_service is not None:
        builtins.extend([
            selected("ReferenceSearchTool", ReferenceSearchTool)(
                reference_service, reference_search_mode,
            ),
            selected("ReferenceGetTool", ReferenceGetTool)(reference_service),
            selected("ReferenceWriteTool", ReferenceWriteTool)(
                reference_service, paper_library_service,
            ),
        ])
    if paper_library_service is not None:
        builtins.extend([
            selected("PaperLibraryLookupTool", PaperLibraryLookupTool)(paper_library_service),
            selected("PaperLibraryDownloadTool", PaperLibraryDownloadTool)(paper_library_service),
            selected("PaperLibraryReadTool", PaperLibraryReadTool)(paper_library_service),
            selected("PaperLibrarySaveTool", PaperLibrarySaveTool)(paper_library_service),
        ])
    if skill_service is not None:
        builtins.append(selected("SkillReadTool", SkillReadTool)(skill_service))
        builtins.append(selected("SkillInstallTool", SkillInstallTool)(
            skill_install_service or skill_service,
        ))
    if cron_service is not None and cron_project_id is not None:
        builtins.append(selected("CronJobTool", CronJobTool)(cron_service, cron_project_id))
    builtins = [
        tool for tool in builtins
        if runtime_profile in getattr(
            tool, "runtime_profiles", ("interactive", "cron", "harness", "maintenance"),
        )
    ]
    registry = AsyncToolRegistry(builtins)
    if subagent_runner is not None:
        register_subagent(registry, subagent_runner, tool_module=tool_module)
    return registry
