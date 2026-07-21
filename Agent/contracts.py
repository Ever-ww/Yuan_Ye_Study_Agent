"""核心层之间共享的不可变数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import AsyncIterator
from typing import Any, Protocol


class EventType(str, Enum):
    """界面与持久化共同使用的运行事件类型。"""

    STARTED = "started"
    TEXT = "text"
    MODEL_RETRY = "model_retry"
    TOOL_REQUESTED = "tool_requested"
    APPROVAL_REQUESTED = "approval_requested"
    TOOL_COMPLETED = "tool_completed"
    COMPRESSION_STARTED = "compression_started"
    CONTEXT_COMPRESSED = "context_compressed"
    COMPRESSION_FALLBACK = "compression_fallback"
    ERROR = "error"
    FINAL = "final"


@dataclass(frozen=True)
class RunEvent:
    """一次运行中可串行消费的事件。"""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次工具调用。"""

    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    """模型服务返回的单次请求精确 Token 用量。"""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ModelReply:
    """模型本轮输出的文本和可选工具调用。"""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finished: bool = True
    usage: TokenUsage | None = None


class ModelProvider(Protocol):
    """所有模型供应商必须实现的异步接口。"""

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[ModelReply]: ...


class ApprovalCallback(Protocol):
    """高风险工具调用的用户确认接口。"""

    async def __call__(self, name: str, arguments: dict[str, Any]) -> bool: ...
