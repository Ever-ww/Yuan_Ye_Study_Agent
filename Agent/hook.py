"""唯一 Hook 契约与项目回调注册入口。"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from context_process import ContextProcessor
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


class HookEvent(BaseModel):
    """回调共享的 Session 上下文；不持久化 Trace 或 Turn 实体。"""

    model_config = ConfigDict(validate_assignment=True, strict=True)

    point: HookPoint
    session_id: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


HookCallback = Callable[[HookEvent], Awaitable[None] | None]


class HookOrigin(str, Enum):
    CORE = "core"
    EXTENSION = "extension"


class HookFailureMode(str, Enum):
    FAIL_CLOSED = "fail_closed"
    ISOLATE = "isolate"


HookOutcomeReporter = Callable[[str, BaseException | None, float], Awaitable[None] | None]


@dataclass(frozen=True)
class HookRegistration:
    priority: int
    order: int
    callback: HookCallback
    identity: str
    origin: HookOrigin
    failure_mode: HookFailureMode
    timeout_seconds: float
    outcome_reporter: HookOutcomeReporter | None = None


class HookExecutor:
    """Apply timeout and isolation consistently to every Hook callback."""

    @staticmethod
    async def _await_in_current_task(
        awaitable: Awaitable[Any], timeout_seconds: float,
    ) -> Any:
        """Timeout a Core Hook without moving ContextVars to a child Task."""
        task = asyncio.current_task()
        if task is None:
            return await awaitable
        timed_out = False

        def cancel_for_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            task.cancel()

        handle = asyncio.get_running_loop().call_later(timeout_seconds, cancel_for_timeout)
        try:
            return await awaitable
        except asyncio.CancelledError as exc:
            if timed_out:
                raise asyncio.TimeoutError() from exc
            raise
        finally:
            handle.cancel()

    async def execute(self, registration: HookRegistration, event: HookEvent) -> None:
        started = monotonic()
        outcome = "success"
        error: BaseException | None = None
        try:
            result = registration.callback(event)
            if inspect.isawaitable(result):
                # wait_for runs the coroutine in a child Task on Python 3.10.
                # Core lifecycle Hooks intentionally share ContextVar scope
                # (TRACE_START installs tokens consumed by TRACE_END), so they
                # must stay in the caller Task. Untrusted Extension callbacks
                # never own that core scope and retain the strict timeout.
                if registration.origin is HookOrigin.EXTENSION:
                    await asyncio.wait_for(result, timeout=registration.timeout_seconds)
                else:
                    await self._await_in_current_task(result, registration.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            outcome, error = "timeout", exc
        except BaseException as exc:
            outcome, error = "exception", exc
        duration = monotonic() - started
        if registration.outcome_reporter is not None:
            try:
                reported = registration.outcome_reporter(outcome, error, duration)
                if inspect.isawaitable(reported):
                    await reported
            except asyncio.CancelledError:
                raise
            except BaseException as report_error:
                if registration.failure_mode is HookFailureMode.FAIL_CLOSED:
                    raise RuntimeError(
                        f"Hook {event.point.value}/{registration.identity} audit failed: "
                        f"{report_error}"
                    ) from report_error
                return
        if error is None or registration.failure_mode is HookFailureMode.ISOLATE:
            return
        raise RuntimeError(
            f"Hook {event.point.value}/{registration.identity} failed: {error}"
        ) from error


class HookRegistry:
    """参考 PI Agent 的事件订阅方式，按优先级和注册顺序执行回调。"""

    def __init__(self, executor: HookExecutor | None = None) -> None:
        self._callbacks: dict[HookPoint, list[HookRegistration]] = defaultdict(list)
        self._order = 0
        self._executor = executor or HookExecutor()

    def register(
        self,
        point: HookPoint,
        callback: HookCallback,
        *,
        priority: int = 0,
        identity: str | None = None,
        origin: HookOrigin = HookOrigin.CORE,
        failure_mode: HookFailureMode | None = None,
        timeout_seconds: float = 30.0,
        outcome_reporter: HookOutcomeReporter | None = None,
    ) -> HookCallback:
        """注册同步或异步回调；优先级数值越小越先执行。"""
        if timeout_seconds <= 0:
            raise ValueError("Hook timeout_seconds must be positive")
        mode = failure_mode or (
            HookFailureMode.ISOLATE if origin is HookOrigin.EXTENSION
            else HookFailureMode.FAIL_CLOSED
        )
        self._callbacks[point].append(HookRegistration(
            priority=priority,
            order=self._order,
            callback=callback,
            identity=identity or getattr(callback, "__name__", type(callback).__name__),
            origin=origin,
            failure_mode=mode,
            timeout_seconds=float(timeout_seconds),
            outcome_reporter=outcome_reporter,
        ))
        self._order += 1
        return callback

    def on(self, point: HookPoint, *, priority: int = 0) -> Callable[[HookCallback], HookCallback]:
        """提供 `registry.on(point)` 装饰器注册形式。"""
        def decorator(callback: HookCallback) -> HookCallback:
            return self.register(point, callback, priority=priority)

        return decorator

    async def emit(self, event: HookEvent) -> HookEvent:
        registrations = sorted(
            self._callbacks.get(event.point, ()),
            key=lambda item: (item.priority, item.order),
        )
        for registration in registrations:
            await self._executor.execute(registration, event)
        return event


async def trace_start(event: HookEvent) -> None:
    """Session 首次开始运行时调用。"""


async def trace_end(event: HookEvent) -> None:
    """Session 本次运行关闭时调用。"""


async def turn_start(event: HookEvent) -> None:
    """一次用户任务在用户消息进入模型上下文前调用。"""


async def turn_end(event: HookEvent) -> None:
    """一次用户任务最终回复持久化后，或终止错误后调用。"""


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


def build_default_hooks(
    memory_dir: Path,
    memory: MemoryStore | None = None,
    context_processor: ContextProcessor | None = None,
    prompts: Any | None = None,
    session_origin: Literal["interactive", "cron", "maintenance"] = "interactive",
) -> HookRegistry:
    """组合项目与记忆回调；Memory 仍只是普通回调集合。"""
    from memory.callbacks import register_memory_callbacks
    from memory.store import MemoryStore

    registry = HookRegistry()
    register_memory_callbacks(
        registry,
        memory or MemoryStore(memory_dir),
        prompts,
        session_origin=session_origin,
    )
    if context_processor is not None:
        from context_process import register_context_callbacks
        register_context_callbacks(registry, context_processor)
    for point, callback in _PROJECT_CALLBACKS.items():
        registry.register(point, callback, priority=-200 if point is HookPoint.TRACE_START else 0)
    return registry


def register_sandbox_callbacks(registry: HookRegistry, sandbox: Any) -> None:
    """把 Docker 生命周期接入正式 Hook；延迟导入避免核心层循环依赖。"""
    from sandbox.callbacks import register_sandbox_callbacks as register

    register(registry, sandbox)
