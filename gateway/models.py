"""Gateway 对外协议与持久化数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
ApprovalState = Literal["pending", "approved", "denied"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "none", "low", "medium", "high", "xhigh", "max",
)


class GatewayEventEnvelope(BaseModel):
    """所有客户端共同消费的、可重放的事件信封。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    version: Literal[1, 2, 3] = 1
    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str | None = None
    run_id: str | None = None
    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    # v2 canonical identity/contract fields. 旧 v1 JSON 没有这些字段，读取时
    # 由 EventStore upcast 到内存投影，历史 canonical bytes 永远不改写。
    command_id: str | None = None
    event_key: str | None = None
    stream_id: str | None = None
    stream_sequence: int | None = Field(default=None, ge=1)
    event_type: str | None = None
    schema_version: int = Field(default=1, ge=1)
    causation_id: str | None = None
    correlation_id: str | None = None
    payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


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
    model_profile_id: str = "default"
    reasoning_effort: ReasoningEffort = "none"
    ui_context: dict[str, Any] | None = None


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


class PaperPatchRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["active", "archived"]


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    project_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    session_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1)
    deadline_at: str | None = None
    model_profile_id: str = Field(default="default", min_length=1, max_length=80)
    reasoning_effort: ReasoningEffort | None = None
    ui_context: "RunUIContext | None" = None


class UIResourceContext(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["paper", "workspace_file"]
    paper_id: str | None = Field(default=None, min_length=1)
    logical_path: str | None = Field(default=None, min_length=1, max_length=4096)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_resource(self) -> "UIResourceContext":
        if self.kind == "paper":
            if not self.paper_id or self.logical_path is not None or self.content_hash is None:
                raise ValueError("Paper UI context requires paper_id and content hash")
        elif (
            self.paper_id is not None
            or not self.logical_path
            or not self.logical_path.casefold().startswith("yyworkspace:\\")
            or self.content_hash is None
        ):
            raise ValueError(
                "Workspace UI context requires a YYWorkspace logical path and content hash",
            )
        return self


class UISelectionContext(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    selected_text: str = Field(default="", max_length=20_000)
    page: int | None = Field(default=None, ge=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_lines(self) -> "UISelectionContext":
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("Selection line range requires both start_line and end_line")
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("Selection end_line must not precede start_line")
        return self


class RunUIContext(BaseModel):
    """Structured, non-tool UI context frozen with one interactive Turn."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    source: Literal["agent", "read", "write"] = "agent"
    resource: UIResourceContext | None = None
    selection: UISelectionContext | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "RunUIContext":
        if self.source == "agent" and (self.resource is not None or self.selection is not None):
            raise ValueError("Agent UI context cannot contain a resource selection")
        if self.source == "read" and (self.resource is None or self.selection is None):
            raise ValueError("Read UI context requires a paper resource and selection")
        if self.source == "read" and self.resource is not None and self.resource.kind != "paper":
            raise ValueError("Read UI context requires a paper resource")
        if self.source == "write" and (
            self.resource is None or self.resource.kind != "workspace_file"
        ):
            raise ValueError("Write UI context requires a workspace_file resource")
        return self


class ModelOption(BaseModel):
    """Non-sensitive model metadata exposed to interactive clients."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    profile_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    selected: bool = False
    reasoning_effort: ReasoningEffort = "low"
    supported_reasoning_efforts: tuple[ReasoningEffort, ...] = REASONING_EFFORTS
    default_reasoning_effort: ReasoningEffort = "low"
    effective_reasoning_effort: ReasoningEffort = "low"

    @field_validator("supported_reasoning_efforts", mode="before")
    @classmethod
    def normalize_supported_reasoning_efforts(cls, value: object) -> object:
        # JSON has no tuple type, so the Gateway wire response is necessarily
        # decoded as a list. Keep the public model immutable after validation.
        if isinstance(value, list):
            return tuple(value)
        return value


class WorkspaceFileWriteRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=5 * 1024 * 1024)
    expected_etag: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkspaceEntryCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    kind: Literal["file", "directory"]


class WorkspaceMoveRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    source: str = Field(min_length=1, max_length=4096)
    destination: str = Field(min_length=1, max_length=4096)


class LatexCompilationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    main_path: str = Field(min_length=1, max_length=4096)
    engine: Literal["auto", "tectonic", "xelatex"] = "auto"
    expected_tree_revision: int | None = Field(default=None, ge=0)


class LatexCompilationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    compilation_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    main_path: str = Field(min_length=1)
    engine: Literal["tectonic", "xelatex"]
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    diagnostics: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


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
    origin_session_id: str | None = None


class HarnessEvolutionDecision(BaseModel):
    """One durable user decision for a Gateway ERROR Evolution proposal."""

    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    confirmed: bool


class HarnessDreamRunRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    selected: str | None = Field(default=None, min_length=1)
    confirmed: bool = False


class HarnessDreamFreezeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    reason: str = Field(default="operator freeze", min_length=1, max_length=1000)


class HarnessDreamDecisionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    approved: bool
    reason: str = Field(min_length=1, max_length=2000)


class HarnessDreamRevertRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    confirmed: bool = False


class CodeTurnRequest(BaseModel):
    """活动 Coding Session 中的一条扩展需求。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    client_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    model_profile_id: str = Field(default="default", min_length=1, max_length=80)
    reasoning_effort: ReasoningEffort | None = None


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
    origin_session_id: str | None = None
    origin_run_id: str | None = None


class CodeTurnResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    message: str
    test_file: str
    attempts: int = Field(ge=1)
    commit: str = ""
    diagnostic: str = ""
    grant_plan: dict[str, Any] = Field(default_factory=dict)
    model_calls: tuple[dict[str, Any], ...] = ()


class CodeFinalizeResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    message: str
    merged: bool = False
    stay_in_code_mode: bool = False
    worktree_path: str = ""
    branch: str = ""
    grant_plan: dict[str, Any] = Field(default_factory=dict)


class ExtensionGrantRequest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    hook_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    source_hash: str = Field(min_length=64, max_length=64)
    manifest_hash: str = Field(min_length=64, max_length=64)
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    tool_contract_hashes: dict[str, str] = Field(default_factory=dict)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ExtensionReenableRequest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    hook_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    source_hash: str = Field(min_length=64, max_length=64)
    expected_revision: int = Field(ge=0)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RuntimeReloadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    actor: str = Field(min_length=1)
    approved_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RuntimePluginRollbackRequest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    plugin_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
    from_generation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1)


class ObserverCorrectionDecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    expected_revision: int = Field(ge=0)
    action: Literal["adopt", "edit", "reject"]
    actor: str = Field(min_length=1)
    edited_prompt: str | None = Field(default=None, max_length=8000)
    reason: str = Field(default="", max_length=2000)


class ObserverSkillCandidateDecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    expected_revision: int = Field(ge=0)
    approved: bool
    actor: str = Field(min_length=1)


def now_iso() -> str:
    """生成带时区、秒级稳定格式的协议时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
