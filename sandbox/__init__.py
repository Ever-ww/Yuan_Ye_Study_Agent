"""Docker 沙箱与本地 checkpoint 的正式公共接口。"""

from .checkpoint import CheckpointStore
from .checkpoint_dream import (
    CheckpointBranchGarbageCollector,
    CheckpointCandidateValidator,
    CheckpointDreamCoordinator,
    CheckpointDreamResult,
    CheckpointDreamSessionResult,
)
from .docker import (
    BashUnavailableError,
    CommandResult,
    DockerSandboxSession,
    DockerUnavailableError,
    SandboxSessionProtocol,
    probe_docker_status,
    sandbox_status_of,
)
from .locks import WorkspaceLockManager
from .models import (
    BashResult,
    CheckpointAuditEvent,
    CheckpointBranchRecord,
    CheckpointMergeAttempt,
    CheckpointRecord,
    CheckpointRestorePoint,
    CheckpointState,
    CheckpointValueAssessment,
    RollbackResult,
    SandboxStatus,
)


def register_sandbox_callbacks(*args, **kwargs):
    """延迟导入 Hook 适配层，避免 Agent 公共入口初始化时形成循环依赖。"""
    from .callbacks import register_sandbox_callbacks as register

    return register(*args, **kwargs)

__all__ = [
    "BashResult",
    "BashUnavailableError",
    "CheckpointAuditEvent",
    "CheckpointBranchGarbageCollector",
    "CheckpointCandidateValidator",
    "CheckpointBranchRecord",
    "CheckpointMergeAttempt",
    "CheckpointRecord",
    "CheckpointDreamCoordinator",
    "CheckpointDreamResult",
    "CheckpointDreamSessionResult",
    "CheckpointRestorePoint",
    "CheckpointState",
    "CheckpointStore",
    "CheckpointValueAssessment",
    "CommandResult",
    "DockerSandboxSession",
    "DockerUnavailableError",
    "RollbackResult",
    "SandboxSessionProtocol",
    "SandboxStatus",
    "WorkspaceLockManager",
    "register_sandbox_callbacks",
    "probe_docker_status",
    "sandbox_status_of",
]
