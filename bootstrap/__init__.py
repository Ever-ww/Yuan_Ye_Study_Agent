"""项目首次运行初始化入口。"""

from .home import (
    legacy_gateway_active,
    legacy_platform_agent_home,
    migrate_source_home,
    platform_agent_home,
)
from .initializer import InitializationResult, ensure_project_initialized, initialize_project, is_project_initialized
from .workspaces import (
    WORKSPACE_MIGRATION_VERSION,
    WORKSPACE_SCHEMA_VERSION,
    default_workspace_root,
    ensure_workspace_initialized,
    workspace_id,
    workspace_manifest,
    workspace_state_dir,
    unregister_workspace,
)

__all__ = [
    "InitializationResult",
    "ensure_project_initialized",
    "initialize_project",
    "is_project_initialized",
    "migrate_source_home",
    "platform_agent_home",
    "legacy_gateway_active",
    "legacy_platform_agent_home",
    "WORKSPACE_MIGRATION_VERSION",
    "WORKSPACE_SCHEMA_VERSION",
    "ensure_workspace_initialized",
    "default_workspace_root",
    "workspace_id",
    "workspace_manifest",
    "workspace_state_dir",
    "unregister_workspace",
]
