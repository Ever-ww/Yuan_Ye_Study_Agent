"""Gateway 对外协议与持久化数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
ApprovalState = Literal["pending", "approved", "denied"]


class GatewayEventEnvelope(BaseModel):
    """所有客户端共同消费的、可重放的事件信封。"""

    model_config = ConfigDict(frozen=True, strict=True)

    version: Literal[1] = 1
    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str | None = None
    run_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class ProjectRecord(BaseModel):
    """一个由 Gateway 管理的工作区。"""

    model_config = ConfigDict(frozen=True, strict=True)

    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    last_opened_at: str = Field(min_length=1)


class RunRecord(BaseModel):
    """一次用户任务在 Gateway 中的生命周期。"""

    model_config = ConfigDict(frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str | None = None
    client_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    status: RunStatus
    created_at: str = Field(min_length=1)
    started_at: str | None = None
    finished_at: str | None = None
    answer: str | None = None
    error: str | None = None
    task_state: str | None = None
    execution_state: str | None = None
    execution_outcome: str | None = None
    finish_reason: str | None = None
    state_revision: int = Field(default=0, ge=0)
    workload_kind: str = "chat"
    recovery_required: bool = False
    terminal_target: str | None = None


class InboxItem(BaseModel):
    """后台运行完成后供任意客户端查看的结果摘要。"""

    model_config = ConfigDict(frozen=True, strict=True)

    item_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str | None = None
    title: str = Field(min_length=1)
    summary: str
    status: RunStatus
    created_at: str = Field(min_length=1)
    read: bool = False


class ApprovalRequest(BaseModel):
    """由工具权限回调挂起、交给发起客户端处理的审批。"""

    model_config = ConfigDict(frozen=True, strict=True)

    approval_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    state: ApprovalState = "pending"
    created_at: str = Field(min_length=1)
    decided_at: str | None = None


class ApprovalDecision(BaseModel):
    """客户端提交的审批结果。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    approved: bool


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1)
    name: str | None = None


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    project_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    session_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1)
    deadline_at: str | None = None


class RecoveryDecisionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    command_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    action: Literal["retry", "confirm_succeeded", "fail", "cancel"]
    operation_id: str | None = None
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    observed_result: str | None = None
    risk_confirmed: bool = False


class BrowserExchangeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    code: str = Field(min_length=1)


class SkillManageRequest(BaseModel):
    """Gateway Skill 安装/更新请求；人工复核通过后可再次提交 confirmed。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    project_id: str = Field(min_length=1)
    action: Literal["install", "update"]
    source: str = Field(min_length=1)
    ref: str | None = None
    skill_path: str | None = None
    name: str | None = None
    confirmed: bool = False


class CodeSessionCreateRequest(BaseModel):
    """由 CLI 发起的持续 Coding Session。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    project_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)


class CodeTurnRequest(BaseModel):
    """活动 Coding Session 中的一条扩展需求。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    task: str = Field(min_length=1)


class CodeSessionRecord(BaseModel):
    """Gateway 对外暴露的 Coding Session 状态。"""

    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    source_root: str = Field(min_length=1)
    worktree_path: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    base_commit: str = Field(min_length=1)
    status: str = Field(min_length=1)
    verified_turns: int = Field(ge=0)


class CodeTurnResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    message: str
    test_file: str
    attempts: int = Field(ge=1)
    commit: str = ""
    diagnostic: str = ""


class CodeFinalizeResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    message: str
    merged: bool = False
    stay_in_code_mode: bool = False
    worktree_path: str = ""
    branch: str = ""


def now_iso() -> str:
    """生成带时区、秒级稳定格式的协议时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
