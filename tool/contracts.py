"""异步工具扩展协议与最小执行上下文。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict


ToolRisk = Literal["read", "write", "high", "dynamic"]


class AsyncTool(Protocol):
    """所有工具实现都必须遵守的稳定协议。"""

    name: str
    description: str
    schema: dict[str, Any]
    risk: ToolRisk
    # 可选声明；未声明时 Registry 根据风险采用保守等级。
    idempotency: str

    async def run(self, arguments: dict[str, Any], context: "ToolContext") -> str: ...

    async def reconcile(self, operation: Any, context: "ToolContext") -> Any: ...


class ToolContext(BaseModel):
    """工具执行时可用的最小受控上下文。"""

    model_config = ConfigDict(frozen=True, strict=True, arbitrary_types_allowed=True)

    project_root: Path
    approval: Callable[[str, dict[str, Any]], Awaitable[bool]] | None = None
    # 运行时注入 Docker/Checkpoint 契约；保持 Any 可避免工具协议反向依赖 Agent 核心。
    sandbox: Any | None = None
    # Runtime 与 Docker 共享的跨进程工作区锁；文件工具禁止自行创建旁路锁。
    file_locks: Any | None = None
    # 当前 Trace/Session 标识只用于审计来源，不参与权限判断。
    session_id: str | None = None
    # Durable Runtime 注入的两阶段副作用协调器；普通/维护 Runtime 可为空。
    operation_coordinator: Any | None = None
