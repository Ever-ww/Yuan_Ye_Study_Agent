"""项目首次运行初始化入口。"""

from .home import (
    legacy_gateway_active,
    legacy_platform_agent_home,
    migrate_source_home,
    platform_agent_home,
)
from .initializer import InitializationResult, ensure_project_initialized, initialize_project, is_project_initialized

__all__ = [
    "InitializationResult",
    "ensure_project_initialized",
    "initialize_project",
    "is_project_initialized",
    "migrate_source_home",
    "platform_agent_home",
    "legacy_gateway_active",
    "legacy_platform_agent_home",
]
