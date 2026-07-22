"""Runtime 内部保留的失败现场；不会直接通过 UI 事件公开。"""

from __future__ import annotations

import copy
import traceback
from dataclasses import dataclass, field
from typing import Any

from Agent.models import ModelNetworkError, ModelResponseFormatError, ModelServiceError
from Agent.errors import AgentExecutionLimitError, AgentInvariantError


@dataclass(frozen=True)
class RuntimeFailure:
    """一次终止性运行错误及其实际模型请求现场。"""

    error: BaseException
    category: str
    traceback_text: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: dict[str, Any] = field(default_factory=dict)
    retry_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def repairable(self) -> bool:
        """首期只允许代码缺陷和模型格式错误进入 Harness。"""
        return self.category in {"code_defect", "model_response_format"}

    @property
    def snapshot_worthy(self) -> bool:
        """仅为可能通过修改代码解决的缺陷保留完整复现快照。"""
        return self.repairable

    @classmethod
    def capture(cls, error: BaseException) -> "RuntimeFailure":
        context = getattr(error, "yy_failure_context", {})
        if not isinstance(context, dict):
            context = {}
        return cls(
            error=error,
            category=_category(error),
            traceback_text="".join(traceback.format_exception(type(error), error, error.__traceback__)),
            messages=copy.deepcopy(context.get("messages", [])),
            tools=copy.deepcopy(context.get("tools", [])),
            model=copy.deepcopy(context.get("model", {})),
            retry_history=copy.deepcopy(context.get("retry_history", [])),
        )


def _category(error: BaseException) -> str:
    if isinstance(error, ModelResponseFormatError):
        return "model_response_format"
    if isinstance(error, ModelNetworkError):
        return "network"
    if isinstance(error, ModelServiceError):
        return "service"
    if isinstance(error, AgentInvariantError):
        return "code_defect"
    if isinstance(error, AgentExecutionLimitError):
        return "operational"
    if isinstance(error, PermissionError):
        return "permission"
    if isinstance(error, (FileNotFoundError, OSError, KeyError, ValueError)):
        return "operational"
    return "code_defect"
