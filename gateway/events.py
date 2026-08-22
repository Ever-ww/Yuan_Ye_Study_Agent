"""Gateway 内存事件广播；持久化由 GatewayStore 负责。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from gateway.models import GatewayEventEnvelope


@dataclass
class EventSubscription:
    client_id: str
    queue: asyncio.Queue[GatewayEventEnvelope]
    run_id: str | None = None


class GatewayEventBus:
    """把事件广播给所有匹配订阅者，慢客户端不会阻塞 Agent。"""

    def __init__(self, *, queue_size: int = 1000) -> None:
        self.queue_size = queue_size
        self._subscriptions: dict[int, EventSubscription] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._client_counts: dict[str, int] = {}
        self._client_ready: dict[str, asyncio.Event] = {}
        self._published_ids: set[str] = set()

    async def subscribe(self, client_id: str, run_id: str | None = None) -> tuple[int, asyncio.Queue[GatewayEventEnvelope]]:
        async with self._lock:
            self._next_id += 1
            subscription_id = self._next_id
            queue: asyncio.Queue[GatewayEventEnvelope] = asyncio.Queue(self.queue_size)
            self._subscriptions[subscription_id] = EventSubscription(client_id, queue, run_id)
            self._client_counts[client_id] = self._client_counts.get(client_id, 0) + 1
            self._client_ready.setdefault(client_id, asyncio.Event()).set()
            return subscription_id, queue

    async def unsubscribe(self, subscription_id: int) -> None:
        async with self._lock:
            subscription = self._subscriptions.pop(subscription_id, None)
            if subscription is None:
                return
            remaining = self._client_counts.get(subscription.client_id, 1) - 1
            if remaining <= 0:
                self._client_counts.pop(subscription.client_id, None)
                self._client_ready.setdefault(subscription.client_id, asyncio.Event()).clear()
            else:
                self._client_counts[subscription.client_id] = remaining

    async def wait_connected(self, client_id: str, timeout_seconds: float = 5.0) -> bool:
        """等待 Run 发起者建立事件连接，防止从未连接的审批永久悬挂。"""
        async with self._lock:
            if self._client_counts.get(client_id, 0) > 0:
                return True
            ready = self._client_ready.setdefault(client_id, asyncio.Event())
        try:
            await asyncio.wait_for(ready.wait(), timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def is_connected(self, client_id: str) -> bool:
        async with self._lock:
            return self._client_counts.get(client_id, 0) > 0

    async def deliver_from_outbox(self, event: GatewayEventEnvelope) -> None:
        """Deliver one already-durable event; business code must not call this API."""
        async with self._lock:
            if event.event_id in self._published_ids:
                return
            self._published_ids.add(event.event_id)
            # 本机 Gateway 的事件量有限；保留最近一批 ID 防止无界增长。
            if len(self._published_ids) > 100_000:
                self._published_ids = {event.event_id}
            subscriptions = tuple(self._subscriptions.values())
        for subscription in subscriptions:
            if subscription.run_id and subscription.run_id != event.run_id:
                continue
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 事件已持久化，客户端可以使用 after_sequence 重放。
                continue
