"""与模型供应商、前端和存储实现解耦的运行时类型协议。

本模块应保持轻量，不导入具体模型、工具、数据库或 UI。其他包可以安全地只依赖这里
的 dataclass 与 ``Protocol``，从而通过结构化类型替换真实实现为测试假对象。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Literal, Protocol
from uuid import uuid4


def utc_now() -> str:
    """返回带 UTC 时区偏移的 ISO 8601 时间，供持久化记录统一排序。"""

    return datetime.now(timezone.utc).isoformat()


class EventType(str, Enum):
    """事件溯源日志使用的稳定事件名称。

    同时继承 ``str`` 使枚举值能直接用于 JSON 和 SQLite；新增事件应保持已有字符串
    不变，否则旧会话将无法被当前运行时正确还原。
    """

    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    USER_MESSAGE = "message.user"
    MODEL_STARTED = "model.started"
    MODEL_DELTA = "model.delta"
    MODEL_COMPLETED = "model.completed"
    TOOL_REQUESTED = "tool.requested"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    FINAL = "run.final"
    ERROR = "run.error"
    COMPACTED = "session.compacted"
    REWOUND = "session.rewound"
    HOOK = "hook"
    MEMORY = "memory"
    SUBAGENT = "subagent"
    CRON = "cron"


@dataclass(frozen=True)
class RunEvent:
    """一次运行时状态变化的不可变事件。

    ``payload`` 按事件类型携带结构化数据；``id`` 用于幂等审计，``created_at`` 使用
    UTC。事件构造后不可修改，持久化内容与前端收到的对象因而保持一致。
    """

    type: EventType | str
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 友好的字典，并将枚举类型正规化为字符串。"""

        value = asdict(self)
        value["type"] = self.type.value if isinstance(self.type, EventType) else self.type
        return value


@dataclass
class Session:
    """一个可恢复会话的当前元数据快照。

    会话自身允许修改，但正常运行时应通过 ``StateStore.update_session`` 持久化变更；
    消息正文和工具轨迹并不存于此对象，而是从事件表重建。
    """

    id: str
    project_root: str
    profile: str = "general"
    title: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    status: str = "active"
    summary: str = ""


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次原生或兼容工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """工具返回给运行时的标准结果。

    ``content`` 是继续喂给模型的文本 Observation；``metadata`` 供 UI 与审计使用，
    ``images`` 可触发后续轮次切换到视觉模型。错误也作为值返回而非直接抛出。
    """

    call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    images: tuple["ImageContent", ...] = ()


@dataclass(frozen=True)
class AgentResult:
    """异步运行时聚合结果，由 ``run_turn`` 的事件流折叠而来。"""

    session_id: str
    answer: str
    completed: bool
    events: list[RunEvent] = field(default_factory=list)


@dataclass(frozen=True)
class ModelMessage:
    """供应商无关的模型消息，覆盖文本、工具关联和内嵌图片。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    images: tuple["ImageContent", ...] = ()


@dataclass(frozen=True)
class ImageContent:
    """无需临时文件即可跨 Provider 传递的 Base64 图片内容。"""

    media_type: str
    data_base64: str


@dataclass(frozen=True)
class ModelOutput:
    """一次完整模型调用的统一输出及可选 token 用量。"""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    provider: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelProvider(Protocol):
    """模型适配器必须实现的异步结构化调用协议。"""

    async def complete(
        self,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0,
    ) -> ModelOutput:
        """完成一次消息请求，并返回文本和零个或多个工具调用。"""

        ...

    async def stream(
        self,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0,
    ) -> AsyncIterator[str]:
        """按增量文本流式输出；不支持流式的实现可在内部模拟。"""

        ...


class AsyncTool(Protocol):
    """权限系统可识别的异步工具结构协议。"""

    name: str
    description: str
    parameters: dict[str, Any]
    risk: str
    sandboxed: bool

    async def run(self, arguments: dict[str, Any], context: Any) -> ToolResult:
        """在调用方完成 Schema 校验和权限审批后执行工具。"""

        ...


class HookHandler(Protocol):
    """可注入 Hook 处理器的最小协议。"""

    async def handle(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """处理生命周期事件并返回受控的结构化修改。"""

        ...


class SandboxProvider(Protocol):
    """命令隔离后端协议；真实 Docker 与测试假实现可互换。"""

    @property
    def available(self) -> bool:
        """指示当前机器是否具备可用隔离后端。"""

        ...

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        writable: bool = False,
        network: bool = False,
        timeout: float = 120,
    ) -> tuple[int, str, str]:
        """返回退出码、标准输出和标准错误，不隐式使用 Shell 解析。"""

        ...


class MemoryStore(Protocol):
    """长期记忆后端的最小写入与检索接口。"""

    def add(self, content: str, *, scope: str, source: str, confidence: float = 1.0) -> str:
        """保存带来源和置信度的事实，返回稳定记忆 ID。"""

        ...

    def search(self, query: str, *, scope: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """在可选作用域中检索最相关且仍有效的记忆。"""

        ...


class SchedulerStore(Protocol):
    """持久计划任务存储的结构协议。"""

    def add_schedule(self, record: dict[str, Any]) -> str:
        """验证并持久化任务记录，返回计划 ID。"""

        ...

    def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出全部计划或仅列出当前启用项。"""

        ...


@dataclass(frozen=True)
class AgentDefinition:
    """Markdown frontmatter 解析后的子代理能力边界。

    ``tools`` 是允许集，``disallowed_tools`` 在其上再次做减法；子代理仍不能绕过
    父运行时的权限审批。``isolation='worktree'`` 表示写操作应使用独立 Git worktree。
    """

    name: str
    description: str
    prompt: str
    model: str | None = None
    max_turns: int = 12
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    memory: str | None = None
    background: bool = False
    isolation: str | None = None
