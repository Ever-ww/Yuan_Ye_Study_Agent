"""Docker 沙箱与分支化本地 checkpoint 的持久化数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BranchStatus = Literal["active", "archived", "merged"]
BranchMergeState = Literal["ready", "blocked", "deferred", "unknown"]
MergeAttemptOutcome = Literal["merged", "blocked", "deferred", "unknown", "skipped"]


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
    """一次不可变的 workspace Git 提交事实。"""

    model_config = ConfigDict(frozen=True, strict=True)

    sequence: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str = Field(min_length=1)
    source: str = Field(min_length=1)
    created_at: datetime
    changes: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    branch_id: str = "legacy-main"
    parent_checkpoint_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merge_parent_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class CheckpointRestorePoint(BaseModel):
    """用户可见、受数量上限约束的精确恢复入口。"""

    model_config = ConfigDict(frozen=True, strict=True)

    sequence: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str = Field(min_length=1)
    created_at: datetime


class CheckpointBranchRecord(BaseModel):
    """独立 checkpoint 仓库中的分支生命周期。"""

    model_config = ConfigDict(frozen=True, strict=True)

    branch_id: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    status: BranchStatus
    merge_state: BranchMergeState | None = None
    merge_eligible: bool = False
    archive_reason: str | None = None
    merge_eligibility_reason: str | None = None
    fork_checkpoint_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    head_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    created_at: datetime
    archived_at: datetime | None = None
    merged_at: datetime | None = None
    gc_after: datetime | None = None
    ref_deleted_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "CheckpointBranchRecord":
        if self.status == "active":
            if self.merge_state is not None or self.merge_eligible:
                raise ValueError("ACTIVE 分支不能携带 merge_state 或 merge_eligible")
        elif self.status == "archived":
            if self.archived_at is None or not self.archive_reason:
                raise ValueError("ARCHIVED 分支必须记录 archived_at 和 archive_reason")
            if self.merge_eligible and self.merge_state is None:
                raise ValueError("可合并的 ARCHIVED 分支必须携带 merge_state")
            if not self.merge_eligible and self.merge_state is not None:
                raise ValueError("不可合并的 ARCHIVED 分支不能携带 merge_state")
        elif self.status == "merged":
            if self.merge_state is not None or self.merged_at is None or self.gc_after is None:
                raise ValueError("MERGED 分支必须记录合并时间且不能携带 merge_state")
        return self


class CheckpointMergeAttempt(BaseModel):
    """一次 Dream merge 的追加式执行证据。"""

    model_config = ConfigDict(frozen=True, strict=True)

    attempt_id: str = Field(min_length=1)
    branch_id: str = Field(min_length=1)
    expected_active_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    archived_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    fork_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    outcome: MergeAttemptOutcome
    reason: str = Field(min_length=1)
    value_assessment: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    completed_at: datetime


class CheckpointPendingMutation(BaseModel):
    """跨 Git ref、状态文件与真实 workspace 的 write-ahead intent。"""

    model_config = ConfigDict(frozen=True, strict=True)

    mutation_id: str = Field(min_length=1)
    kind: Literal["create", "rollback", "merge"]
    stage: Literal["intent", "state_switched"]
    old_branch_id: str
    old_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    target_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    checkpoint_sequence: int | None = Field(default=None, ge=1)
    checkpoint_source: str | None = None
    checkpoint_metadata: dict[str, Any] = Field(default_factory=dict)
    new_branch_id: str | None = None
    archived_branch_id: str | None = None
    merge_attempt_id: str | None = None
    merge_checkpoint_sequence: int | None = Field(default=None, ge=1)
    value_assessment: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "CheckpointPendingMutation":
        if self.kind == "create":
            if self.checkpoint_sequence is None or not self.checkpoint_source:
                raise ValueError("create mutation必须保存checkpoint sequence和source")
        elif self.old_head_sha is None:
            raise ValueError("rollback/merge mutation必须保存旧活动HEAD")
        return self


class CheckpointAuditEvent(BaseModel):
    """Checkpoint图创建、分叉、合并和淘汰的本地审计事件。"""

    model_config = ConfigDict(frozen=True, strict=True)

    action: Literal[
        "created", "evicted", "rollback", "restored", "branch_forked",
        "merge_assessed", "merge_committed", "merge_blocked", "branch_ref_gc",
        "mutation_recovered",
    ]
    timestamp: datetime
    checkpoint_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    details: dict[str, Any] = Field(default_factory=dict)


class CheckpointState(BaseModel):
    """Session checkpoint v2索引；Git对象仍由独立裸仓库保存。"""

    model_config = ConfigDict(strict=True)

    version: Literal[2] = 2
    session_id: str = Field(min_length=1)
    workspace_root: str
    active_branch_id: str
    next_sequence: int = Field(default=1, ge=1)
    next_branch_sequence: int = Field(default=2, ge=2)
    commit_records: list[CheckpointRecord] = Field(default_factory=list)
    restore_points: list[CheckpointRestorePoint] = Field(default_factory=list)
    branches: list[CheckpointBranchRecord] = Field(default_factory=list)
    merge_attempts: list[CheckpointMergeAttempt] = Field(default_factory=list)
    pending_mutation: CheckpointPendingMutation | None = None
    events: list[CheckpointAuditEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> "CheckpointState":
        active = [item for item in self.branches if item.status == "active"]
        if len(active) != 1 or active[0].branch_id != self.active_branch_id:
            raise ValueError("CheckpointState 必须且只能有一个活动分支")
        branch_ids = [item.branch_id for item in self.branches]
        if len(branch_ids) != len(set(branch_ids)):
            raise ValueError("checkpoint branch_id 必须唯一")
        sequences = [item.sequence for item in self.commit_records]
        if len(sequences) != len(set(sequences)):
            raise ValueError("checkpoint sequence 必须唯一")
        visible = [item.sequence for item in self.restore_points]
        if len(visible) != len(set(visible)):
            raise ValueError("restore point sequence 必须唯一")
        return self


class CheckpointValueAssessment(BaseModel):
    """无记忆Dream对归档分支价值的结构化判断。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision: Literal["MERGE", "SKIP", "NEEDS_REVIEW"]
    reason: str = Field(min_length=1, max_length=2000)
    valuable_changes: tuple[str, ...] = ()
    risk_summary: str = Field(default="", max_length=2000)


class BashResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    exit_code: int
    output: str
    checkpoint: CheckpointRecord | None = None


class RollbackResult(BaseModel):
    """分叉式回退结果；removed仅为一版兼容字段。"""

    model_config = ConfigDict(frozen=True, strict=True)

    restored: CheckpointRecord
    archived_branch: CheckpointBranchRecord
    new_active_branch: CheckpointBranchRecord
    preserved_future: tuple[CheckpointRecord, ...] = ()
    removed: tuple[CheckpointRecord, ...] = ()
