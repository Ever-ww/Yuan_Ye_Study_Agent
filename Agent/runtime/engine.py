"""会话、事件持久化和 ReAct 主循环编排。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from Agent.config import RuntimeConfig, load_runtime_config
from Agent.contracts import ApprovalCallback, EventType, RunEvent
from Agent.extensions import HookRegistry
from Agent.models import build_provider
from Agent.react import ReactLoop
from memory import MemoryStore
from prompt import PromptComposer
from tools import AsyncToolRegistry, ToolContext, default_tools


@dataclass(frozen=True)
class RuntimeResult:
    """聚合后的最终运行结果。"""

    answer: str
    session_id: str
    completed: bool


class AgentRuntime:
    """项目唯一正式的异步 Agent 运行时。"""

    def __init__(self, config: RuntimeConfig | None = None, *, provider=None, tools: AsyncToolRegistry | None = None, memory: MemoryStore | None = None, hooks: HookRegistry | None = None, approval: ApprovalCallback | None = None) -> None:
        self.config = config or load_runtime_config()
        self.memory = memory or MemoryStore(self.config.memory_dir)
        self.tools = tools or default_tools(self.config.project_root)
        self.provider = provider or build_provider(self.config.provider, self.config.model, base_url=self.config.base_url, api_key=self.config.api_key, stream=self.config.stream)
        self.hooks, self.approval = hooks or HookRegistry(), approval
        self.prompts = PromptComposer(self.config.project_root, self.memory)

    async def run_turn(self, task: str, session_id: str | None = None) -> AsyncIterator[RunEvent]:
        """执行一个任务，并按发生顺序产生可被 UI 消费的事件。"""
        session_id = session_id or self.memory.create_session(task)
        yield RunEvent(EventType.STARTED, {"session_id": session_id})
        messages = self.prompts.compose(task, session_id)
        self.memory.record_user(session_id, task)
        context = ToolContext(project_root=self.config.project_root, approval=self.approval)
        loop = ReactLoop(self.provider, self.tools, self.hooks, self.config.max_steps)
        final = ""
        try:
            async for event in loop.run(messages, context):
                if event.type is EventType.FINAL:
                    final = str(event.payload["answer"])
                yield event
            if final:
                self.memory.record_assistant(session_id, final)
        except Exception as exc:
            message = str(exc) or f"{type(exc).__name__}：运行时发生未提供详情的异常"
            yield RunEvent(EventType.ERROR, {"message": message})

    async def run(self, task: str, session_id: str | None = None) -> RuntimeResult:
        """消费事件流并返回调用方常用的聚合结果。"""
        active_id, answer, completed = session_id or "", "", False
        async for event in self.run_turn(task, active_id):
            if event.type is EventType.STARTED:
                active_id = str(event.payload["session_id"])
            if event.type is EventType.FINAL:
                answer, completed = str(event.payload["answer"]), True
        return RuntimeResult(answer, active_id, completed)
