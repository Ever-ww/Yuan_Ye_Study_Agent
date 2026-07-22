"""精简运行时：只编排 Session 生命周期、Hook 与 ReAct 事件。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from Agent.config import RuntimeConfig, load_runtime_config
from Agent.contracts import ApprovalCallback, EventType, RunEvent
from Agent.hook import HookEvent, HookPoint, HookRegistry, build_default_hooks
from Agent.models import build_provider
from Agent.react import ReactLoop
from Agent.retry import ModelRetryPolicy
from context_process import ContextProcessor
from memory import MemoryStore
from prompt import PromptComposer
from tools import AsyncToolRegistry, ToolContext, default_tools
from tools.subagent import SubagentTool
from .subagent import RuntimeSubagentRunner
from .failure import RuntimeFailure


class RuntimeResult(BaseModel):
    """聚合后的最终运行结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

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
        context_processor: ContextProcessor | None = None,
        compression_provider_factory=None,
        subagent_runner=None,
        enable_context_processing: bool = True,
        enable_subagent: bool = True,
        retry_policy: ModelRetryPolicy | None = None,
        raise_errors: bool = False,
    ) -> None:
        self.config = config or load_runtime_config()
        self.provider = provider or build_provider(
            self.config.provider,
            self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            stream=self.config.stream,
        )
        self.approval = approval
        self.retry_policy = retry_policy
        self.raise_errors = raise_errors
        self.last_failure: RuntimeFailure | None = None
        self.memory = memory or MemoryStore(self.config.memory_dir)
        if tools is not None:
            self.tools = tools
        else:
            base_tools = default_tools(self.config.project_root)
            if enable_subagent:
                runner = subagent_runner or RuntimeSubagentRunner(self.config, base_tools)
                risks = {name: base_tools.risk_of(name) for name in base_tools.names()}
                base_tools.register(SubagentTool(runner, risks))
            self.tools = base_tools
        self.context_processor = None
        if enable_context_processing:
            self.context_processor = context_processor or ContextProcessor(
                self.config,
                self.memory,
                provider_factory=compression_provider_factory,
            )
        self.hooks = hooks or build_default_hooks(self.config.memory_dir, self.memory, self.context_processor)
        self.prompts = PromptComposer(self.config.project_root)
        self._session_id: str | None = None
        self._session_open = False

    @property
    def active_session_id(self) -> str | None:
        """返回当前打开的 Session，供 CLI 在失败后保存复现现场。"""
        return self._session_id

    async def run_task(self, task: str, session_id: str | None = None) -> AsyncIterator[RunEvent]:
        """处理一次用户输入；内部每次模型 API 调用各自形成一个 Turn。"""
        self.last_failure = None
        if task.strip() == "/compress":
            async for event in self._compress_command(session_id):
                yield event
            return
        try:
            active_id = await self._ensure_session(task, session_id)
        except Exception as exc:
            self.last_failure = RuntimeFailure.capture(exc)
            if self.raise_errors:
                raise
            yield RunEvent(type=EventType.ERROR, payload={"message": str(exc) or type(exc).__name__})
            return
        yield RunEvent(type=EventType.STARTED, payload={"session_id": active_id})
        messages = self.prompts.compose(task)
        context = ToolContext(project_root=self.config.project_root, approval=self.approval)
        loop = ReactLoop(
            self.provider,
            self.tools,
            self.hooks,
            self.config.max_steps,
            retry_policy=self.retry_policy,
        )
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
            self.last_failure = RuntimeFailure.capture(exc)
            if self.raise_errors:
                raise
            message = str(exc) or f"{type(exc).__name__}：运行时发生未提供详情的异常"
            yield RunEvent(type=EventType.ERROR, payload={"message": message})

    async def _compress_command(self, session_id: str | None) -> AsyncIterator[RunEvent]:
        """由主 Runtime 处理手动压缩命令，不把命令写入 Session。"""
        if self.context_processor is None:
            yield RunEvent(type=EventType.ERROR, payload={"message": "当前 Runtime 未启用上下文压缩"})
            return
        requested = session_id or self._session_id
        if not requested:
            yield RunEvent(type=EventType.ERROR, payload={"message": "当前没有可压缩会话"})
            return
        try:
            active_id = await self._ensure_session("", str(requested))
        except Exception as exc:
            yield RunEvent(type=EventType.ERROR, payload={"message": str(exc) or type(exc).__name__})
            return
        yield RunEvent(type=EventType.STARTED, payload={"session_id": active_id})
        yield RunEvent(type=EventType.COMPRESSION_STARTED, payload={"session_id": active_id})
        result = await self.context_processor.compress(active_id)
        if result.status == "error":
            yield RunEvent(type=EventType.ERROR, payload={"message": result.message})
            return
        event_type = EventType.CONTEXT_COMPRESSED if result.status == "compressed" else EventType.COMPRESSION_FALLBACK
        yield RunEvent(type=event_type, payload=result.payload())
        yield RunEvent(type=EventType.FINAL, payload={"answer": result.message, "completed": True})

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
        return RuntimeResult(answer=answer, session_id=active_id, completed=completed)

    async def close(self, error: Exception | None = None) -> None:
        """关闭当前 Session 运行范围并且只触发一次 trace_end。"""
        if not self._session_open or self._session_id is None:
            return
        session_id = self._session_id
        try:
            await self.hooks.emit(HookEvent(point=HookPoint.TRACE_END, session_id=session_id, data={"error": error}))
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
        event = HookEvent(point=HookPoint.TRACE_START, session_id=session_id, data={"task": task, "new_session": requested_session_id is None})
        await self.hooks.emit(event)
        self._session_id = event.session_id
        self._session_open = True
        return event.session_id
