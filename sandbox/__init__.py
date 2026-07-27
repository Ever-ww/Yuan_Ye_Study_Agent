"""Docker 沙箱与本地 checkpoint 的正式公共接口。"""

from .checkpoint import CheckpointStore
from .docker import CommandResult, DockerSandboxSession, SandboxSessionProtocol
from .locks import WorkspaceLockManager
from .models import (
    BashResult,
    CheckpointAuditEvent,
    CheckpointRecord,
    CheckpointState,
    RollbackResult,
)


def register_sandbox_callbacks(*args, **kwargs):
    """延迟导入 Hook 适配层，避免 Agent 公共入口初始化时形成循环依赖。"""
    from .callbacks import register_sandbox_callbacks as register

    return register(*args, **kwargs)

__all__ = [
    "BashResult",
    "CheckpointAuditEvent",
    "CheckpointRecord",
    "CheckpointState",
    "CheckpointStore",
    "CommandResult",
    "DockerSandboxSession",
    "RollbackResult",
    "SandboxSessionProtocol",
    "WorkspaceLockManager",
    "register_sandbox_callbacks",
]
