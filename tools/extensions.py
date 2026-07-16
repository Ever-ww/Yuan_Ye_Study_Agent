"""将 Memory、资料库、Skills、Cron、子代理与外部协议接入运行时。

本模块只负责把各子系统的结构化 API 适配成 ``AsyncTool``；
实际的持久化、索引、调度和进程管理仍由对应管理器完成。
工具声明的 ``risk`` 只是审批策略的输入，不会也不应越过
Agent Runtime 的权限决策。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from Agent.integrations import LSPManager, MCPManager
from Agent.permissions import CapabilityGrant
from Agent.scheduler import SQLiteSchedulerStore
from Agent.types import ToolResult
from memory.store import CorpusStore, SQLiteMemoryStore
from skills.registry import SkillRegistry

from .harness import BaseTool, ToolContext


@dataclass
class MemorySearchTool(BaseTool):
    """在可审计的长期记忆中检索相关事实。

    可选 ``scope`` 用于限制会话、项目、用户或子代理作用域；
    返回 JSON 而非自然语言，以便模型保留来源、置信度等字段。
    """

    memory: SQLiteMemoryStore | None = None
    name: str = "memory_search"
    description: str = "Search auditable long-term memory."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"query": {"type": "string"}, "scope": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """执行结构化记忆检索，并将结果序列化为 JSON。"""

        del context
        assert self.memory
        results = self.memory.search(str(arguments["query"]), scope=arguments.get("scope"), limit=int(arguments.get("limit", 8)))
        return ToolResult("", self.name, json.dumps(results, ensure_ascii=False, indent=2))


@dataclass
class MemoryWriteTool(BaseTool):
    """将明确的事实写入可审计长期记忆。

    这是中风险工具，因为错误信息会影响未来会话。存储层的
    ``default_scope`` 如果已经由 Runtime 绑定，优先级高于模型参数，
    避免工具调用自行扩大记忆作用域。
    """

    memory: SQLiteMemoryStore | None = None
    name: str = "memory_write"
    description: str = "Write an explicit auditable memory."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"content": {"type": "string"}, "scope": {"type": "string", "enum": ["session", "project", "user", "agent"]}}, "required": ["content"]})
    risk: str = "medium"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """保存记忆，并记录当前会话为来源以便追溯。"""

        assert self.memory
        requested_scope = arguments.get("scope")
        # Runtime 预绑定的作用域是权限上限，不接受模型参数覆盖。
        scope = self.memory.default_scope if self.memory.default_scope else str(requested_scope or "project")
        memory_id = self.memory.add(str(arguments["content"]), scope=scope, source=f"agent:{context.session_id}", confidence=0.8)
        return ToolResult("", self.name, f"已保存记忆 {memory_id}")


@dataclass
class CorpusSearchTool(BaseTool):
    """检索独立的学习资料索引。

    Corpus 与用户长期记忆彻底分离：结果应包含文件路径、
    页码或章节等引用信息，供最终回答引用原始资料。
    """

    corpus: CorpusStore | None = None
    name: str = "corpus_search"
    description: str = "Search indexed study documents and return source paths and pages."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """执行全文检索并返回保留来源字段的 JSON。"""

        del context
        assert self.corpus
        results = self.corpus.search(str(arguments["query"]), int(arguments.get("limit", 8)))
        return ToolResult("", self.name, json.dumps(results, ensure_ascii=False, indent=2))


@dataclass
class SkillLoadTool(BaseTool):
    """按需加载已发现 Skill 的详细指令。

    注册表平时只把名称和描述放入 Prompt，直到模型确定需要
    某个 Skill 时才读取 ``SKILL.md``，以避免长指令占满上下文。
    """

    skills: SkillRegistry | None = None
    name: str = "skill"
    description: str = "Load a registered skill's instructions on demand."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """返回技能主体内容，并在 metadata 中保留路径和作用域。"""

        del context
        assert self.skills
        skill = self.skills.get(str(arguments["name"]))
        if not skill:
            return ToolResult("", self.name, "技能不存在", True)
        return ToolResult("", self.name, skill.load(), metadata={"path": str(skill.path), "scope": skill.scope})


@dataclass
class CronCreateTool(BaseTool):
    """创建带固定能力包的持久化 Cron 任务。

    这里不会把当前 Agent 的全部权限隐式传给后台任务；
    仅将用户审批的工具、路径、域名和命令前缀写入
    :class:`CapabilityGrant`。调度器恢复任务时必须继续遵守该上限。
    """

    scheduler: SQLiteSchedulerStore | None = None
    plugin_capability_snapshot: dict[str, dict[str, str]] = field(default_factory=dict)
    name: str = "cron_create"
    description: str = "Create a persistent scheduled prompt with an explicit capability grant."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"cron": {"type": "string"}, "prompt": {"type": "string"}, "timezone": {"type": "string"}, "recurring": {"type": "boolean"}, "tools": {"type": "array", "items": {"type": "string"}}, "paths": {"type": "array", "items": {"type": "string"}}, "domains": {"type": "array", "items": {"type": "string"}}, "command_prefixes": {"type": "array", "items": {"type": "string"}}}, "required": ["cron", "prompt"]})
    risk: str = "high"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """将调度参数和不可自行扩展的能力包持久化。"""

        assert self.scheduler
        grant = CapabilityGrant(
            tools=tuple(arguments.get("tools", [])), paths=tuple(arguments.get("paths", [])),
            domains=tuple(arguments.get("domains", [])), command_prefixes=tuple(arguments.get("command_prefixes", [])),
            # 深复制快照，避免长生命周期 Runtime 中插件管理状态变化后修改已创建任务的边界。
            plugin_capability_snapshot={
                plugin_id: dict(metadata)
                for plugin_id, metadata in self.plugin_capability_snapshot.items()
            },
        )
        schedule_id = self.scheduler.add_schedule({**arguments, "capability": grant, "session_id": context.session_id})
        return ToolResult("", self.name, f"已创建 Cron 任务 {schedule_id}")


@dataclass
class CronListTool(BaseTool):
    """列出已持久化的调度任务及其当前状态。"""

    scheduler: SQLiteSchedulerStore | None = None
    name: str = "cron_list"
    description: str = "List scheduled prompts."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """以 JSON 返回调度器中的任务列表。"""

        del arguments, context
        assert self.scheduler
        return ToolResult("", self.name, json.dumps(self.scheduler.list_schedules(), ensure_ascii=False, indent=2))


@dataclass
class CronDeleteTool(BaseTool):
    """按 ID 删除持久化调度任务。"""

    scheduler: SQLiteSchedulerStore | None = None
    name: str = "cron_delete"
    description: str = "Delete a scheduled prompt by ID."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
    risk: str = "medium"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """删除目标任务，并用 ``is_error`` 区分任务不存在。"""

        del context
        assert self.scheduler
        deleted = self.scheduler.delete(str(arguments["id"]))
        return ToolResult("", self.name, "已删除" if deleted else "任务不存在", not deleted)


# 运行器由主 Agent 注入，使工具层不反向依赖具体的子代理实现。
SubagentRunner = Callable[[str, str, CapabilityGrant | None], Awaitable[str]]


@dataclass
class SubagentTool(BaseTool):
    """在独立上下文中运行已注册的专用子代理。

    子代理只返回其最终答案文本，且不能借由此工具代替用户审批。
    实际的工具上限、轮次上限和隔离方式由注入的 runner 决定。
    """

    runner: SubagentRunner | None = None
    name: str = "agent_spawn"
    description: str = "Run a specialized subagent in an isolated conversation and return its summary."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"agent": {"type": "string"}, "task": {"type": "string"}}, "required": ["agent", "task"]})
    risk: str = "medium"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """调用子代理运行器，未配置时返回可诊断错误。"""

        if not self.runner:
            return ToolResult("", self.name, "子代理运行器不可用", True)
        answer = await self.runner(
            str(arguments["agent"]),
            str(arguments["task"]),
            context.capability_grant,
        )
        return ToolResult("", self.name, answer)


@dataclass
class MCPCallTool(BaseTool):
    """调用经显式配置和信任的 MCP 服务器工具。

    MCP 返回值可能是 Pydantic 模型或普通 Python 对象，因此
    适配层会在不丢失结构的前提下序列化。高风险标记确保
    跨进程或远程调用仍会进入 Runtime 审批链。
    """

    manager: MCPManager | None = None
    name: str = "mcp_call"
    description: str = "Call an explicitly configured MCP server tool."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["server", "tool"]})
    risk: str = "high"

    def permission_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """把逻辑 server 名扩展为权限系统可审计的真实连接目标。

        本方法只读取初始化时已加载的配置，不连接网络、不启动 SDK，也不运行容器。
        ``command``/``argv`` 与 ``url`` 保持顶层字段，使 ``PermissionBroker`` 的命令
        前缀和域名能力规则可以直接匹配；``config_hash`` 则让同名服务配置变化后无法
        继续复用旧的精确 allow 规则。
        """

        if not self.manager:
            raise RuntimeError("MCP 管理器不可用")
        descriptor = self.manager.authorization_descriptor(str(arguments["server"]))
        return {**arguments, **descriptor}

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """将服务器名、工具名和参数交给 MCP 管理器执行。"""

        del context
        assert self.manager
        value = await self.manager.call_tool(str(arguments["server"]), str(arguments["tool"]), dict(arguments.get("arguments", {})))
        # MCP SDK 的新版响应通常是 Pydantic v2 模型，先规整为普通容器。
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        return ToolResult("", self.name, json.dumps(value, ensure_ascii=False, default=str))


@dataclass
class LSPTool(BaseTool):
    """将常用语言服务器请求映射为统一工具。

    工具只提供 hover、定义、引用、诊断和工作区符号这些
    受控操作，不暴露任意 JSON-RPC method，从而缩小模型可触达的
    服务器功能面。
    """

    manager: LSPManager | None = None
    name: str = "lsp"
    description: str = "Query a configured language server for hover, definition, references, diagnostics, or workspace symbols."
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {"server": {"type": "string"}, "operation": {"type": "string", "enum": ["hover", "definition", "references", "diagnostics", "symbols"]}, "uri": {"type": "string"}, "line": {"type": "integer"}, "character": {"type": "integer"}, "query": {"type": "string"}}, "required": ["server", "operation"]})
    risk: str = "medium"

    def permission_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """返回包含实际容器命令与配置哈希的无副作用审批参数。"""

        if not self.manager:
            raise RuntimeError("LSP 管理器不可用")
        descriptor = self.manager.authorization_descriptor(str(arguments["server"]))
        return {**arguments, **descriptor}

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """确保语言服务器已启动，构造对应 JSON-RPC 参数并发起请求。"""

        del context
        assert self.manager
        server, operation = str(arguments["server"]), str(arguments["operation"])
        await self.manager.start(server)
        # 模型只能选择这个固定映射，不能注入任意 LSP method 名。
        mapping = {
            "hover": "textDocument/hover", "definition": "textDocument/definition",
            "references": "textDocument/references", "diagnostics": "textDocument/diagnostic",
            "symbols": "workspace/symbol",
        }
        if operation == "symbols":
            params = {"query": str(arguments.get("query", ""))}
        else:
            params = {"textDocument": {"uri": str(arguments.get("uri", ""))}}
            if operation in {"hover", "definition", "references"}:
                params["position"] = {"line": int(arguments.get("line", 0)), "character": int(arguments.get("character", 0))}
            if operation == "references":
                params["context"] = {"includeDeclaration": True}
        value = await self.manager.request(server, mapping[operation], params)
        return ToolResult("", self.name, json.dumps(value, ensure_ascii=False, default=str))
