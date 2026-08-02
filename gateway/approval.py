"""把工具审批转换为可断线处理的 Gateway 请求。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable
from uuid import uuid4

from gateway.models import ApprovalRequest, now_iso
from gateway.store import GatewayStore


_RUN_CONTEXT: ContextVar[tuple[str, str] | None] = ContextVar("gateway_run_context", default=None)
PublishApproval = Callable[[ApprovalRequest], Awaitable[None]]
WaitForClient = Callable[[str], Awaitable[bool]]


class GatewayApprovalBroker:
    """Runtime 可长期复用，当前 Run 身份通过 ContextVar 隔离。"""

    def __init__(
        self,
        store: GatewayStore,
        publish: PublishApproval,
        wait_for_client: WaitForClient | None = None,
    ) -> None:
        self.store = store
        self.publish = publish
        self.wait_for_client = wait_for_client
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[bool]]] = {}
        self._lock = asyncio.Lock()

    def bind_run(self, run_id: str, client_id: str) -> Token:
        return _RUN_CONTEXT.set((run_id, client_id))

    def reset_run(self, token: Token) -> None:
        _RUN_CONTEXT.reset(token)

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        context = _RUN_CONTEXT.get()
        if context is None:
            return False
        run_id, client_id = context
        if client_id.startswith("cron:"):
            # 定时任务无人值守，危险能力不得等待或继承创建者的历史授权。
            return False
        request = ApprovalRequest(
            approval_id=uuid4().hex,
            run_id=run_id,
            client_id=client_id,
            tool_name=tool_name,
            arguments=arguments,
            created_at=now_iso(),
        )
        future = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._pending[request.approval_id] = (request, future)
        self.store.save_approval(request)
        try:
            await self.publish(request)
            if self.wait_for_client is not None and not await self.wait_for_client(client_id):
                self.store.decide_approval(request.approval_id, False)
                if not future.done():
                    future.set_result(False)
            return await future
        finally:
            # 发布失败、Run 取消或 Gateway 关闭都必须把数据库中的 pending 终结掉。
            self.store.decide_approval(request.approval_id, False)
            async with self._lock:
                self._pending.pop(request.approval_id, None)

    async def decide(self, approval_id: str, client_id: str, approved: bool) -> bool:
        async with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                raise KeyError(f"审批不存在或已经结束：{approval_id}")
            request, future = pending
            if request.client_id != client_id:
                raise PermissionError("只有发起任务的客户端可以处理本次审批")
            self.store.decide_approval(approval_id, approved)
            if not future.done():
                future.set_result(approved)
            return approved

    async def deny_client(self, client_id: str) -> int:
        async with self._lock:
            selected = [
                (approval_id, future)
                for approval_id, (request, future) in self._pending.items()
                if request.client_id == client_id
            ]
            for approval_id, future in selected:
                self.store.decide_approval(approval_id, False)
                if not future.done():
                    future.set_result(False)
        return len(selected)

    async def deny_all(self) -> None:
        async with self._lock:
            selected = list(self._pending.items())
            for approval_id, (_, future) in selected:
                self.store.decide_approval(approval_id, False)
                if not future.done():
                    future.set_result(False)
