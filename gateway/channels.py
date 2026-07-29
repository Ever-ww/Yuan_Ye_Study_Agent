"""未来飞书等渠道复用的最小适配器契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from gateway.models import GatewayEventEnvelope


class ChannelAdapter(Protocol):
    """渠道只负责身份、收发和审批能力，不直接访问 AgentRuntime。"""

    name: str

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def receive(self) -> AsyncIterator[dict[str, Any]]: ...

    def map_identity(self, external_identity: str) -> str: ...

    def route(self, identity: str, payload: dict[str, Any]) -> str: ...

    async def deliver(self, target: str, event: GatewayEventEnvelope) -> None: ...

    async def can_approve(self, identity: str, payload: dict[str, Any]) -> bool: ...
