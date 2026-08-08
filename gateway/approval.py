"""把工具审批转换为可断线处理的 Gateway 请求。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from gateway.models import ApprovalRequest, now_iso
from gateway.audit import AuditSanitizer
from Agent.state import (
    CreateApprovalCommand,
    DecideApprovalCommand,
    DurableApproval,
    ExpireApprovalCommand,
)
from gateway.durable_execution import current_attempt_id, current_operation_id
from gateway.state_controller import StateController
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
        state_controller: StateController | None = None,
        approval_timeout_seconds: int = 600,
    ) -> None:
        self.store = store
        self.publish = publish
        self.wait_for_client = wait_for_client
        self.state_controller = state_controller
        self.approval_timeout_seconds = max(1, approval_timeout_seconds)
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
        if self.state_controller is not None:
            operation_id = current_operation_id()
            attempt_id = current_attempt_id()
            if operation_id is None or attempt_id is None:
                raise RuntimeError("Durable Approval 缺少对应的 Operation")
            operation = self.state_controller.operation(operation_id)
            attempt = self.state_controller.current_attempt(operation_id)
            if attempt.attempt_id != attempt_id:
                raise RuntimeError("Durable Approval Attempt 已变化")
            expires = (
                datetime.now().astimezone() + timedelta(seconds=self.approval_timeout_seconds)
            ).isoformat(timespec="seconds")
            state = self.state_controller.state(run_id)
            self.state_controller.apply(CreateApprovalCommand(
                command_id=uuid4().hex,
                run_id=run_id,
                expected_revision=state.revision,
                gateway_epoch=self.state_controller.gateway_epoch,
                approval=DurableApproval(
                    approval_id=request.approval_id,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    attempt_no=attempt.attempt_no,
                    stable_key=f"approval:{operation.stable_key}:{attempt.attempt_no}",
                    request_hash=operation.request_hash,
                    run_id=run_id,
                    client_id=client_id,
                    tool_name=tool_name,
                    arguments_hash=_arguments_hash(arguments),
                    arguments_json=_arguments_audit_json(arguments),
                    created_at=request.created_at,
                    expires_at=expires,
                ),
            ))
        else:
            self.store.save_approval(request)
        try:
            await self.publish(request)
            if self.wait_for_client is not None and not await self.wait_for_client(client_id):
                await self.decide(request.approval_id, client_id, False)
                if not future.done():
                    future.set_result(False)
            try:
                return await asyncio.wait_for(future, timeout=self.approval_timeout_seconds)
            except asyncio.TimeoutError:
                if self.state_controller is not None:
                    state = self.state_controller.state(run_id)
                    self.state_controller.apply(ExpireApprovalCommand(
                        command_id=uuid4().hex,
                        run_id=run_id,
                        expected_revision=state.revision,
                        gateway_epoch=self.state_controller.gateway_epoch,
                        approval_id=request.approval_id,
                    ))
                else:
                    self.store.decide_approval(request.approval_id, False)
                return False
        finally:
            # SQLite Approval 才是权威状态；任务取消或 Gateway 重启不自动拒绝。
            async with self._lock:
                self._pending.pop(request.approval_id, None)

    async def decide(self, approval_id: str, client_id: str, approved: bool) -> bool:
        async with self._lock:
            pending = self._pending.get(approval_id)
            if self.state_controller is not None:
                approval = self.state_controller.approval(approval_id)
                if approval.client_id != client_id:
                    raise PermissionError("只有发起任务的客户端可以处理本次审批")
                state = self.state_controller.state(approval.run_id)
                result = self.state_controller.apply(DecideApprovalCommand(
                    command_id=f"approval:{approval_id}:{'approve' if approved else 'deny'}",
                    run_id=approval.run_id,
                    expected_revision=state.revision,
                    gateway_epoch=self.state_controller.gateway_epoch,
                    approval_id=approval_id,
                    approved=approved,
                    decided_by=client_id,
                    reason="客户端决定",
                ))
                approved = result.approval is not None and result.approval.status.value == "approved"
                future = pending[1] if pending is not None else None
            else:
                if pending is None:
                    raise KeyError(f"审批不存在或已经结束：{approval_id}")
                request, future = pending
                if request.client_id != client_id:
                    raise PermissionError("只有发起任务的客户端可以处理本次审批")
                self.store.decide_approval(approval_id, approved)
            if future is not None and not future.done():
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
                if self.state_controller is not None:
                    approval = self.state_controller.approval(approval_id)
                    state = self.state_controller.state(approval.run_id)
                    self.state_controller.apply(DecideApprovalCommand(
                        command_id=f"approval:{approval_id}:disconnect-deny",
                        run_id=approval.run_id,
                        expected_revision=state.revision,
                        gateway_epoch=self.state_controller.gateway_epoch,
                        approval_id=approval_id,
                        approved=False,
                        decided_by=client_id,
                        reason="发起客户端断开",
                    ))
                else:
                    self.store.decide_approval(approval_id, False)
                if not future.done():
                    future.set_result(False)
        return len(selected)

    async def deny_all(self) -> None:
        if self.state_controller is not None:
            # Gateway 重启后未过期审批继续等待；只取消进程内 waiter。
            async with self._lock:
                for _, future in self._pending.values():
                    if not future.done():
                        future.cancel()
            return
        async with self._lock:
            selected = list(self._pending.items())
            for approval_id, (_, future) in selected:
                self.store.decide_approval(approval_id, False)
                if not future.done():
                    future.set_result(False)


def _arguments_json(arguments: dict[str, Any]) -> str:
    import json
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _arguments_hash(arguments: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(_arguments_json(arguments).encode("utf-8")).hexdigest()


def _arguments_audit_json(arguments: dict[str, Any]) -> str:
    import json
    return json.dumps(
        AuditSanitizer.sanitize(arguments),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
