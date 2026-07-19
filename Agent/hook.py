"""唯一 Hook 契约与项目回调注册入口。"""

from __future__ import annotations

import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from memory.store import MemoryStore


class HookPoint(str, Enum):
    """Agent 可插入回调的十个固定生命周期位置。"""

    TRACE_START = "trace_start"
    TRACE_END = "trace_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    MODEL_BEFORE = "model_before"
    MODEL_DURING = "model_during"
    MODEL_AFTER = "model_after"
    TOOL_BEFORE = "tool_before"
    TOOL_DURING = "tool_during"
    TOOL_AFTER = "tool_after"


@dataclass
class HookEvent:
    """回调共享的 Session 上下文；不持久化 Trace 或 Turn 实体。"""

    point: HookPoint
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)


HookCallback = Callable[[HookEvent], Awaitable[None] | None]


class HookRegistry:
    """参考 PI Agent 的事件订阅方式，按优先级和注册顺序执行回调。"""

    def __init__(self) -> None:
        self._callbacks: dict[HookPoint, list[tuple[int, int, HookCallback]]] = defaultdict(list)
        self._order = 0

    def register(self, point: HookPoint, callback: HookCallback, *, priority: int = 0) -> HookCallback:
        """注册同步或异步回调；优先级数值越小越先执行。"""
        self._callbacks[point].append((priority, self._order, callback))
        self._order += 1
        return callback

    def on(self, point: HookPoint, *, priority: int = 0) -> Callable[[HookCallback], HookCallback]:
        """提供 `registry.on(point)` 装饰器注册形式。"""
        def decorator(callback: HookCallback) -> HookCallback:
            return self.register(point, callback, priority=priority)

        return decorator

    async def emit(self, event: HookEvent) -> HookEvent:
        """依次执行回调，并为失败补充 Hook 阶段与函数名。"""
        callbacks = sorted(self._callbacks.get(event.point, ()), key=lambda item: (item[0], item[1]))
        for _, _, callback in callbacks:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                name = getattr(callback, "__name__", type(callback).__name__)
                raise RuntimeError(f"Hook {event.point.value}/{name} 执行失败：{exc}") from exc
        return event


async def trace_start(event: HookEvent) -> None:
    """Session 首次开始运行时调用。"""


async def trace_end(event: HookEvent) -> None:
    """Session 本次运行关闭时调用。"""


async def turn_start(event: HookEvent) -> None:
    """一次模型 API 调用及其后续工具阶段开始时调用。"""


async def turn_end(event: HookEvent) -> None:
    """该模型调用及其请求的全部工具完成后调用。"""


async def model_before(event: HookEvent) -> None:
    """模型请求发送前调用，可修改 event.data 中的 messages/tools。"""


async def model_during(event: HookEvent) -> None:
    """模型请求即将进入真实 Provider 时调用一次。"""


async def model_after(event: HookEvent) -> None:
    """模型请求成功或失败后调用，结果或异常位于 event.data。"""


async def tool_before(event: HookEvent) -> None:
    """工具校验与执行前调用，可修改 event.data['arguments']。"""


async def tool_during(event: HookEvent) -> None:
    """工具即将进入真实执行函数时调用一次。"""


async def tool_after(event: HookEvent) -> None:
    """工具成功或失败后调用，结果或异常位于 event.data。"""


_PROJECT_CALLBACKS = {
    HookPoint.TRACE_START: trace_start,
    HookPoint.TRACE_END: trace_end,
    HookPoint.TURN_START: turn_start,
    HookPoint.TURN_END: turn_end,
    HookPoint.MODEL_BEFORE: model_before,
    HookPoint.MODEL_DURING: model_during,
    HookPoint.MODEL_AFTER: model_after,
    HookPoint.TOOL_BEFORE: tool_before,
    HookPoint.TOOL_DURING: tool_during,
    HookPoint.TOOL_AFTER: tool_after,
}


def build_default_hooks(memory_dir: Path, memory: MemoryStore | None = None) -> HookRegistry:
    """组合项目与记忆回调；Memory 仍只是普通回调集合。"""
    from memory.callbacks import register_memory_callbacks
    from memory.store import MemoryStore

    registry = HookRegistry()
    register_memory_callbacks(registry, memory or MemoryStore(memory_dir))
    for point, callback in _PROJECT_CALLBACKS.items():
        registry.register(point, callback, priority=-200 if point is HookPoint.TRACE_START else 0)
    return registry
