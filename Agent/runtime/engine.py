"""精简运行时：只编排 Session 生命周期、Hook 与 ReAct 事件。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from Agent.config import RuntimeConfig, load_runtime_config
from Agent.contracts import ApprovalCallback, EventType, RunEvent
from Agent.hook import HookEvent, HookPoint, HookRegistry, build_default_hooks
from Agent.models import build_provider
from Agent.react import ReactLoop
from prompt import PromptComposer
from tools import AsyncToolRegistry, ToolContext, default_tools


@dataclass(frozen=True)
class RuntimeResult:
    """聚合后的最终运行结果。"""

    answer: str
    session_id: str
    completed: bool


class AgentRuntime:
    """不直接读写记忆的异步 Agent 编排器。"""

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        provider=None,
        tools: AsyncToolRegistry | None = None,
        memory=None,
        hooks: HookRegistry | None = None,
        approval: ApprovalCallback | None = None,
    ) -> None:
        self.config = config or load_runtime_config()
        self.tools = tools or default_tools(self.config.project_root)
        self.provider = provider or build_provider(
            self.config.provider,
            self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            stream=self.config.stream,
        )
        self.hooks = hooks or build_default_hooks(self.config.memory_dir, memory)
        self.approval = approval
        self.prompts = PromptComposer(self.config.project_root)
        self._session_id: str | None = None
        self._session_open = False

    async def run_task(self, task: str, session_id: str | None = None) -> AsyncIterator[RunEvent]:
        """处理一次用户输入；内部每次模型 API 调用各自形成一个 Turn。"""
        try:
            active_id = await self._ensure_session(task, session_id)
        except Exception as exc:
            yield RunEvent(EventType.ERROR, {"message": str(exc) or type(exc).__name__})
            return
        yield RunEvent(EventType.STARTED, {"session_id": active_id})
        messages = self.prompts.compose(task)
        context = ToolContext(project_root=self.config.project_root, approval=self.approval)
        loop = ReactLoop(self.provider, self.tools, self.hooks, self.config.max_steps)
        model = {
            "provider": self.config.provider,
            "name": self.config.model,
            "base_url": self.config.base_url,
            "stream": self.config.stream,
        }
        try:
            async for event in loop.run(messages, context, task=task, session_id=active_id, model=model):
                yield event
        except Exception as exc:
            message = str(exc) or f"{type(exc).__name__}：运行时发生未提供详情的异常"
            yield RunEvent(EventType.ERROR, {"message": message})

    async def run(self, task: str, session_id: str | None = None) -> RuntimeResult:
        """运行单次用户任务，完成后触发 trace_end 并返回聚合结果。"""
        active_id, answer, completed = session_id or "", "", False
        try:
            async for event in self.run_task(task, session_id):
                if event.type is EventType.STARTED:
                    active_id = str(event.payload["session_id"])
                elif event.type is EventType.FINAL:
                    answer, completed = str(event.payload["answer"]), True
        finally:
            await self.close()
        return RuntimeResult(answer, active_id, completed)

    async def close(self, error: Exception | None = None) -> None:
        """关闭当前 Session 运行范围并且只触发一次 trace_end。"""
        if not self._session_open or self._session_id is None:
            return
        session_id = self._session_id
        try:
            await self.hooks.emit(HookEvent(HookPoint.TRACE_END, session_id, data={"error": error}))
        finally:
            self._session_open = False
            self._session_id = None

    async def __aenter__(self) -> "AgentRuntime":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close(exc)

    async def _ensure_session(self, task: str, requested_session_id: str | None) -> str:
        """首次使用 Runtime 时打开 Session 并触发概念上的 trace_start。"""
        if self._session_open:
            if requested_session_id and requested_session_id != self._session_id:
                raise ValueError("同一个 AgentRuntime 不能在未关闭时切换 Session")
            return str(self._session_id)
        session_id = requested_session_id or uuid4().hex[:16]
        event = HookEvent(HookPoint.TRACE_START, session_id, data={"task": task, "new_session": requested_session_id is None})
        await self.hooks.emit(event)
        self._session_id = event.session_id
        self._session_open = True
        return event.session_id
