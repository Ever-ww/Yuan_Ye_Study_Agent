"""Docker 沙箱与本地 Git checkpoint 的 Pydantic 数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SandboxStatus(BaseModel):
    """当前 Trace 的 Docker/Checkpoint 能力状态。"""

    model_config = ConfigDict(frozen=True, strict=True)

    mode: Literal["pending", "docker", "checkpoint_only", "closed"]
    bash_available: bool
    reason_code: str | None = None
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capabilities(self) -> "SandboxStatus":
        if self.bash_available != (self.mode == "docker"):
            raise ValueError("只有 docker 模式可以声明 Bash 可用")
        return self


class CheckpointRecord(BaseModel):
    """一个仅存在于本机独立 Git 对象库中的工作区快照。"""

    model_config = ConfigDict(frozen=True, strict=True)

    sequence: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str = Field(min_length=1)
    source: str = Field(min_length=1)
    created_at: datetime
    changes: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckpointAuditEvent(BaseModel):
    """checkpoint 创建、淘汰和恢复的持久化本机审计事件。"""

    model_config = ConfigDict(frozen=True, strict=True)

    action: Literal["created", "evicted", "rollback", "restored"]
    timestamp: datetime
    checkpoint_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    details: dict[str, Any] = Field(default_factory=dict)


class CheckpointState(BaseModel):
    """Session checkpoint 索引；Git 对象仍由独立仓库保存。"""

    model_config = ConfigDict(strict=True)

    version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    next_sequence: int = Field(default=1, ge=1)
    checkpoints: list[CheckpointRecord] = Field(default_factory=list)
    events: list[CheckpointAuditEvent] = Field(default_factory=list)


class BashResult(BaseModel):
    """一次 Docker Bash 调用的结构化结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    exit_code: int
    output: str
    checkpoint: CheckpointRecord | None = None


class RollbackResult(BaseModel):
    """一次 hard-reset 语义回溯的结构化结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    restored: CheckpointRecord
    removed: tuple[CheckpointRecord, ...] = ()
