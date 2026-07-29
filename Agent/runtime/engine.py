"""精简运行时：只编排 Session 生命周期、Hook 与 ReAct 事件。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from Agent.config import RuntimeConfig, load_runtime_config
from Agent.contracts import ApprovalCallback, EventType, RunEvent
from Agent.hook import (
    HookEvent,
    HookPoint,
    HookRegistry,
    build_default_hooks,
    register_sandbox_callbacks,
)
from Agent.models import build_provider
from Agent.react import ReactLoop
from Agent.retry import ModelRetryPolicy
from context_process import ContextProcessor
from memory import MemoryStore
from prompt import PromptComposer
from sandbox import DockerSandboxSession, SandboxSessionProtocol, WorkspaceLockManager
from skill import SkillService
from tools import AsyncToolRegistry, ToolContext, default_tools, register_subagent
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
        tool_context: ToolContext | None = None,
        context_processor: ContextProcessor | None = None,
        skills: SkillService | None = None,
        compression_provider_factory=None,
        subagent_runner=None,
        enable_context_processing: bool = True,
        enable_skills: bool = True,
        enable_subagent: bool = True,
        sandbox: SandboxSessionProtocol | None = None,
        file_locks: WorkspaceLockManager | None = None,
        enable_sandbox: bool = True,
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
        if tool_context is not None and approval is not None:
            raise ValueError("tool_context 与 approval 不可同时传入")
        if tool_context is not None and tool_context.project_root.resolve() != self.config.workspace_root:
            raise ValueError("ToolContext 工作区必须与 RuntimeConfig.workspace_root 一致")
        context_sandbox = tool_context.sandbox if tool_context is not None else None
        if sandbox is not None and context_sandbox is not None and sandbox is not context_sandbox:
            raise ValueError("sandbox 与 ToolContext.sandbox 不可指向不同实例")
        selected_sandbox = context_sandbox if context_sandbox is not None else sandbox
        context_locks = tool_context.file_locks if tool_context is not None else None
        sandbox_locks = getattr(selected_sandbox, "file_locks", None)
        candidates = [value for value in (context_locks, file_locks, sandbox_locks) if value is not None]
        if candidates and any(value is not candidates[0] for value in candidates[1:]):
            raise ValueError("Runtime、ToolContext 与 sandbox 必须共享同一个文件锁管理器")
        self.file_locks = candidates[0] if candidates else WorkspaceLockManager(
            self.config.workspace_root,
            state_root=self.config.agent_root,
        )
        self._owns_sandbox = False
        if context_sandbox is not None:
            self.sandbox = context_sandbox
        elif sandbox is not None:
            self.sandbox = sandbox
            self._owns_sandbox = True
        elif enable_sandbox:
            self.sandbox = DockerSandboxSession(
                self.config.workspace_root,
                state_root=self.config.agent_root,
                checkpoint_limit=self.config.sandbox_checkpoint_limit,
                file_locks=self.file_locks,
            )
            self._owns_sandbox = True
        else:
            self.sandbox = None
        if tool_context is None:
            self.tool_context = ToolContext(
                project_root=self.config.workspace_root,
                approval=approval,
                sandbox=self.sandbox,
                file_locks=self.file_locks,
            )
        else:
            updates = {}
            if tool_context.sandbox is None and self.sandbox is not None:
                updates["sandbox"] = self.sandbox
            if tool_context.file_locks is None:
                updates["file_locks"] = self.file_locks
            self.tool_context = tool_context.model_copy(update=updates) if updates else tool_context
        self.approval = self.tool_context.approval
        self.retry_policy = retry_policy
        self.raise_errors = raise_errors
        self.last_failure: RuntimeFailure | None = None
        if memory is None:
            self.memory = MemoryStore(
                self.config.memory_dir,
                workspace_root=self.config.workspace_root,
                agent_root=self.config.agent_root,
            )
        else:
            if isinstance(memory, MemoryStore):
                if memory.agent_root != self.config.agent_root:
                    raise ValueError("MemoryStore.agent_root 必须与 RuntimeConfig.agent_root 一致")
                if memory.workspace_root != self.config.workspace_root:
                    raise ValueError("MemoryStore.workspace_root 必须与 RuntimeConfig.workspace_root 一致")
            self.memory = memory
        if enable_skills:
            if skills is not None:
                if skills.agent_root != self.config.agent_root:
                    raise ValueError("SkillService.agent_root 必须与 RuntimeConfig.agent_root 一致")
                if skills.workspace_root != self.config.workspace_root:
                    raise ValueError("SkillService.workspace_root 必须与 RuntimeConfig.workspace_root 一致")
                self.skills = skills
            else:
                self.skills = SkillService(
                    self.config.agent_root,
                    self.config.workspace_root,
                    approval=self.approval,
                )
        else:
            self.skills = None
        if tools is not None:
            self.tools = tools
        else:
            base_tools = default_tools(self.config.workspace_root, skill_service=self.skills)
            if enable_subagent:
                runner = subagent_runner or RuntimeSubagentRunner(self.config, base_tools)
                register_subagent(base_tools, runner)
            self.tools = base_tools
        self.context_processor = None
        if enable_context_processing:
            self.context_processor = context_processor or ContextProcessor(
                self.config,
                self.memory,
                provider_factory=compression_provider_factory,
            )
        self.prompts = PromptComposer(self.config, self.memory, self.skills, sandbox_enabled=self.sandbox is not None)
        self.hooks = hooks or build_default_hooks(
            self.config.memory_dir, self.memory, self.context_processor, self.prompts,
        )
        if self._owns_sandbox and self.sandbox is not None:
            register_sandbox_callbacks(self.hooks, self.sandbox)
        self._session_id: str | None = None
        self._session_open = False

    @property
    def active_session_id(self) -> str | None:
        """返回当前打开的 Session，供 CLI 在失败后保存复现现场。"""
        return self._session_id

    async def run_task(self, task: str, session_id: str | None = None) -> AsyncIterator[RunEvent]:
        """处理一次用户输入；一个 Turn 覆盖完整的用户任务。"""
        self.last_failure = None
        if task.strip() == "/compress":
            async for event in self._compress_command(session_id):
                yield event
            return
        if task.strip() == "/context refresh":
            async for event in self._refresh_context_command(session_id):
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
        turn_started = False
        model = {
            "provider": self.config.provider,
            "name": self.config.model,
            "base_url": self.config.base_url,
            "stream": self.config.stream,
        }
        try:
            await self.hooks.emit(HookEvent(
                point=HookPoint.TURN_START,
                session_id=active_id,
                data={"task": task, "config": self.config},
            ))
            turn_started = True
            messages = self.prompts.compose(task, active_id)
        except Exception as exc:
            self.last_failure = RuntimeFailure.capture(exc)
            if self.raise_errors:
                raise
            yield RunEvent(type=EventType.ERROR, payload={"message": str(exc) or type(exc).__name__})
            return
        loop = ReactLoop(
            self.provider,
            self.tools,
            self.hooks,
            self.config.max_steps,
            retry_policy=self.retry_policy,
        )
        final_payload: dict[str, object] | None = None
        failure: BaseException | None = None
        try:
            async for event in loop.run(messages, self.tool_context, task=task, session_id=active_id, model=model):
                if event.type is EventType.FINAL:
                    final_payload = dict(event.payload)
                yield event
        except asyncio.CancelledError as exc:
            failure = exc
            raise
        except Exception as exc:
            failure = exc
            self.last_failure = RuntimeFailure.capture(exc)
            if self.raise_errors:
                raise
            message = str(exc) or f"{type(exc).__name__}：运行时发生未提供详情的异常"
            yield RunEvent(type=EventType.ERROR, payload={"message": message})
        finally:
            if turn_started:
                payload: dict[str, object] = {
                    "task": task,
                    "model": model,
                    "error": failure,
                    "completed": failure is None and final_payload is not None,
                    "cancelled": isinstance(failure, asyncio.CancelledError),
                }
                if final_payload is not None:
                    payload.update(final_payload)
                await self.hooks.emit(HookEvent(point=HookPoint.TURN_END, session_id=active_id, data=payload))

    async def _refresh_context_command(self, session_id: str | None) -> AsyncIterator[RunEvent]:
        """显式重新读取当前 Session 的文件上下文，不写入 JSONL。"""
        requested = session_id or self._session_id
        if not requested:
            yield RunEvent(type=EventType.ERROR, payload={"message": "当前没有可刷新的会话"})
            return
        active_id = await self._ensure_session("", str(requested))
        self.memory.refresh_messages(active_id)
        self.prompts.refresh(active_id)
        yield RunEvent(type=EventType.FINAL, payload={"answer": "上下文缓存已刷新", "completed": True})

    def refresh_skills(self) -> int:
        """显式重新扫描已审核 Skill，并刷新当前 System Prompt。"""
        if self.skills is None:
            raise RuntimeError("当前 Runtime 已禁用 Skill")
        count = len(self.skills.catalog())
        if self._session_id is not None:
            self.prompts.refresh(self._session_id)
        return count

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
        if result.status == "compressed":
            self.prompts.refresh(active_id)
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
            self.prompts.close(session_id)
            if self._owns_sandbox and self.sandbox is not None:
                await self.sandbox.close()
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
        try:
            await self.hooks.emit(event)
        except Exception:
            # trace_start 中后续 Memory/项目 Hook 失败时，Runtime 尚未标记打开，
            # 因此必须在这里显式回收已经启动的容器。
            if self._owns_sandbox and self.sandbox is not None:
                await self.sandbox.close()
            raise
        if not self.memory.has_session(event.session_id):
            self.memory.create_session(task, session_id=event.session_id)
        self._session_id = event.session_id
        self._session_open = True
        return event.session_id
