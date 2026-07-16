"""异步、事件驱动的 Agent Harness 主运行时。

``AgentRuntime`` 是模型、Prompt、工具、权限、Hook、记忆、资料库、Skill、插件、Cron、
MCP/LSP、子代理与团队能力的组合根。一次 turn 以 ``RunEvent`` 流对外暴露，并同步
写入 SQLite，使 CLI 与 Web 能消费相同状态，也让中断后的会话可从事件重新构造。

本模块刻意不把组件实现揉进主循环：模型、沙箱、存储和审批均可注入假实现进行测试；
工具执行固定经过 Schema 校验 → PreToolUse Hook → PermissionBroker → 工具 →
PostToolUse Hook，任何扩展都不能绕开这条安全链。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from .subagents import AgentRegistry
from .teams import TeamStore
from .config import RuntimeConfig, load_runtime_config
from tools.extensions import (
    CorpusSearchTool, CronCreateTool, CronDeleteTool, CronListTool, MemorySearchTool,
    MemoryWriteTool, SkillLoadTool, SubagentTool, MCPCallTool, LSPTool,
)
from .hooks import HookEngine
from .integrations import LSPManager, MCPManager
from memory.store import CorpusStore, SQLiteMemoryStore
from model_choice.provider import FallbackModelProvider, LegacyModelProvider
from .permissions import ApprovalCallback, CapabilityGrant, PermissionBroker, plugin_capability_snapshot
from prompt.composer import PromptComposer
from .sandbox import DockerSandbox
from .scheduler import SQLiteSchedulerStore
from skills.registry import PluginManager, SkillRegistry, load_plugin_manifest
from .storage import StateStore
from tools.harness import ToolContext, ToolRegistry, default_tools
from .types import AgentResult, EventType, ImageContent, ModelMessage, ModelProvider, RunEvent, Session, ToolResult


class AgentRuntime:
    """本地 Harness 的长生命周期组合对象和单轮执行器。

    一个实例可以串行服务多个会话，也可同时服务不同会话；同一会话的并发 turn 会被
    ``_active_sessions`` 拒绝，以维护事件顺序、压缩摘要和文件回滚的一致性。
    """

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        provider: ModelProvider | None = None,
        store: StateStore | None = None,
        approval_callback: ApprovalCallback | None = None,
        question_callback: Callable[[str, list[str]], Awaitable[str]] | None = None,
    ) -> None:
        """构造并连接所有 Harness 组件，不发起模型调用或执行第三方代码。

        ``provider``、``store`` 和交互回调可注入，便于离线测试。未注入模型时，根据
        分层配置创建主/备用适配器；插件只加载已安装且启用项，并对可执行组件逐项应用
        ``trusted_components_json``，普通 Skill 文本发现与可执行信任彼此独立。
        """

        self.config = config or load_runtime_config()
        self.store = store or StateStore(self.config.state_db)
        if provider is None:
            # FallbackProvider 在模型层切换，不会让主运行时重复已完成的工具调用。
            primary = LegacyModelProvider(self.config.model, providers=self.config.providers, timeout=self.config.timeout_seconds)
            fallback = LegacyModelProvider(self.config.fallback_model, providers=self.config.providers, timeout=self.config.timeout_seconds) if self.config.fallback_model else None
            provider = FallbackModelProvider(primary, fallback)
        self.provider = provider
        self.vision_provider: ModelProvider | None = None
        if self.config.vision_model:
            # 仅当消息中实际出现图片时才启用视觉模型，普通文本保持主模型选择。
            self.vision_provider = LegacyModelProvider(self.config.vision_model, providers=self.config.providers, timeout=self.config.timeout_seconds)
        self.question_callback = question_callback
        self.sandbox = DockerSandbox(self.config.sandbox.docker_image, allow_unsandboxed=self.config.sandbox.allow_unsandboxed_commands)
        self.permissions = PermissionBroker(self.store, self.config.project_root, self.config.permission_mode, approval_callback)
        self.memory = SQLiteMemoryStore(self.store, self.config.state_dir / "memory")
        self.corpus = CorpusStore(self.store)
        self.skills = SkillRegistry(self.config)
        self.plugins = PluginManager(self.config, self.store)
        plugin_rows = self.plugins.installed(enabled_only=True)
        current_plugin_capabilities = plugin_capability_snapshot(plugin_rows)
        plugin_roots = [Path(row["installed_path"]) for row in plugin_rows]
        # Skill 文本可被渐进发现；Hooks/Agent/MCP/LSP 只有记录为信任的组件才进入管理器。
        trusted: dict[str, list[Path]] = {name: [] for name in ("hooks", "agents", "mcp", "lsp")}
        for row in plugin_rows:
            root = Path(row["installed_path"])
            components = set(json.loads(row["trusted_components_json"]))
            for component in trusted:
                if component in components:
                    trusted[component].append(root)
        self.skills.discover(plugin_roots)
        self.scheduler = SQLiteSchedulerStore(self.store)
        self.agent_registry = AgentRegistry(self.config.project_root, self.config.user_dir)
        self.agent_registry.discover(trusted["agents"])
        self.teams = TeamStore(self.store)
        mcp_files = [(root / ".mcp.json", str(load_plugin_manifest(root).get("name", root.name))) for root in trusted["mcp"]]
        lsp_files = [(root / ".lsp.json", str(load_plugin_manifest(root).get("name", root.name))) for root in trusted["lsp"]]
        # MCP stdio 与 LSP 复用 Runtime 的同一 Docker 配置，避免管理器退回默认镜像或主机进程。
        self.mcp = MCPManager(self.config.project_root, self.config.user_dir, mcp_files, sandbox=self.sandbox)
        self.lsp = LSPManager(self.config.project_root, self.config.user_dir, lsp_files, sandbox=self.sandbox)
        self.prompts = PromptComposer(self.config.project_root, self.config.profile)
        hook_files = [root / "hooks" / "hooks.json" for root in trusted["hooks"] if (root / "hooks" / "hooks.json").exists()]
        self.hooks = HookEngine(self.config.project_root, self.sandbox, prompt_handler=self._prompt_hook, extra_paths=hook_files, allowed_domains=tuple(self.config.sandbox.allowed_domains))
        # 内置低层工具和高层 Harness 工具进入同一个异步注册表，统一 Schema 与权限路径。
        tools = default_tools(self.config.web_search_url) + [
            MemorySearchTool(memory=self.memory), MemoryWriteTool(memory=self.memory),
            CorpusSearchTool(corpus=self.corpus), SkillLoadTool(skills=self.skills),
            CronCreateTool(
                scheduler=self.scheduler,
                plugin_capability_snapshot=current_plugin_capabilities,
            ),
            CronListTool(scheduler=self.scheduler), CronDeleteTool(scheduler=self.scheduler),
            SubagentTool(runner=self.run_subagent),
            MCPCallTool(manager=self.mcp), LSPTool(manager=self.lsp),
        ]
        self.tools = ToolRegistry(tools)
        self._active_sessions: set[str] = set()

    def create_session(self, *, title: str = "", profile: str | None = None) -> Session:
        """在当前项目创建会话；未指定 profile 时使用运行时默认角色。"""

        return self.store.create_session(str(self.config.project_root), profile or self.config.profile, title)

    async def run_turn(
        self,
        task: str,
        *,
        session_id: str | None = None,
        capability_grant: CapabilityGrant | None = None,
        allowed_tools: set[str] | None = None,
    ) -> AsyncIterator[RunEvent]:
        """执行一个用户 turn，并按发生顺序异步产出持久化事件。

        ``session_id`` 为空会创建新会话；``capability_grant`` 限制无人值守执行上限；
        ``allowed_tools`` 是子代理白名单。生成器提前取消时 ``finally`` 仍会释放会话锁，
        已写事件继续保留，便于下一次调用恢复上下文。
        """

        if not task.strip():
            raise ValueError("任务不能为空")
        session = self.store.get_session(session_id) if session_id else None
        if session_id and not session:
            raise KeyError(f"会话不存在：{session_id}")
        session = session or self.create_session(title=task[:80])
        # 事件表和摘要都假设同一会话线性演进，因此禁止重入同一 session。
        if session.id in self._active_sessions:
            raise RuntimeError("同一会话不能并发执行两个 turn")
        self._active_sessions.add(session.id)
        try:
            # 状态写入也必须位于 finally 保护范围；若 SQLite 在这里失败，内存会话锁仍会释放。
            self.store.update_session(session.id, status="running")
            if capability_grant and capability_grant.plugin_capability_snapshot is not None:
                current_snapshot = plugin_capability_snapshot(self.plugins.installed(enabled_only=True))
                expected_snapshot = capability_grant.plugin_capability_snapshot
                if current_snapshot != expected_snapshot:
                    changed = sorted(
                        plugin_id
                        for plugin_id in set(current_snapshot) | set(expected_snapshot)
                        if current_snapshot.get(plugin_id) != expected_snapshot.get(plugin_id)
                    )
                    yield self._emit(
                        EventType.ERROR,
                        session.id,
                        {
                            "error": "后台任务固定的插件集合、内容或信任状态已变化：" + ", ".join(changed),
                            "needs_approval": True,
                        },
                    )
                    return
            elif capability_grant and capability_grant.plugin_versions:
                # 兼容旧 capability_json：旧格式只能校验已列出的内容哈希，无法表达完整集合与
                # 信任状态。保留读取能力，新的 Cron 一律写入上面的完整快照字段。
                current_versions = {
                    str(row["id"]): str(row["content_hash"])
                    for row in self.plugins.installed(enabled_only=True)
                }
                changed = sorted(
                    plugin_id
                    for plugin_id, pinned_hash in capability_grant.plugin_versions.items()
                    if current_versions.get(plugin_id) != pinned_hash
                )
                if changed:
                    yield self._emit(
                        EventType.ERROR,
                        session.id,
                        {
                            "error": "旧后台任务固定的插件内容已变化或被禁用：" + ", ".join(changed),
                            "needs_approval": True,
                        },
                    )
                    return
            # SessionStart 只对从未产生事件的新会话触发，恢复会话不会重复初始化 Hook。
            if not self.store.events(session.id):
                yield self._emit(EventType.SESSION_STARTED, session.id, {"profile": session.profile})
                await self.hooks.emit("SessionStart", {"session_id": session.id})
            prompt_hook = await self.hooks.emit("UserPromptSubmit", {"session_id": session.id, "prompt": task})
            if not prompt_hook.allowed:
                yield self._emit(EventType.ERROR, session.id, {"error": prompt_hook.message})
                return
            # Hook 可以重写提示文本，但它无权在此指定工具审批状态。
            task = str((prompt_hook.payload or {}).get("prompt", task))
            yield self._emit(EventType.USER_MESSAGE, session.id, {"content": task})
            messages = self._messages(session)
            # Prompt 每轮重新组合，及时反映项目指令、记忆索引和 Skill 目录变化。
            system_prompt, _ = self.prompts.compose(
                memory_index=self.memory.index_text(), skill_catalog=self.skills.catalog(), summary=session.summary,
            )
            messages.insert(0, ModelMessage("system", system_prompt))
            if len(messages) > self.config.context_event_limit:
                # 压缩先持久化摘要，再发 COMPACTED 事件作为后续重建边界。
                compacted_messages = await self._compact(session, messages)
                if compacted_messages is not messages:
                    # ``_compact`` 在 BeforeCompact Hook 拒绝时返回原列表对象。只有实际
                    # 生成并持久化摘要后才发布 COMPACTED，避免事件日志虚报压缩成功。
                    messages = compacted_messages
                    yield self._emit(EventType.COMPACTED, session.id, {"summary": self.store.get_session(session.id).summary})
            schemas = self.tools.schemas(allowed_tools)
            for step in range(1, self.config.max_steps + 1):
                before = await self.hooks.emit("BeforeModel", {"session_id": session.id, "step": step})
                if not before.allowed:
                    yield self._emit(EventType.ERROR, session.id, {"error": before.message})
                    return
                yield self._emit(EventType.MODEL_STARTED, session.id, {"step": step})
                try:
                    # 图片工具结果只影响后续轮次，不改变无图消息的默认 Provider。
                    active_provider = self.vision_provider if self.vision_provider and any(message.images for message in messages) else self.provider
                    output = await active_provider.complete(messages, schemas, temperature=self.config.temperature)
                except Exception as exc:
                    yield self._emit(EventType.ERROR, session.id, {"error": str(exc), "step": step})
                    return
                if output.content:
                    # Provider 的统一 ``complete`` 可能一次返回全文；按固定块模拟前端增量事件。
                    for offset in range(0, len(output.content), 120):
                        yield self._emit(EventType.MODEL_DELTA, session.id, {"delta": output.content[offset:offset + 120], "step": step})
                yield self._emit(EventType.MODEL_COMPLETED, session.id, {
                    "content": output.content, "model": output.model, "provider": output.provider,
                    "input_tokens": output.input_tokens, "output_tokens": output.output_tokens,
                    "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in output.tool_calls],
                })
                await self.hooks.emit("AfterModel", {"session_id": session.id, "step": step, "content": output.content})
                if not output.tool_calls:
                    # 无工具调用即视为本轮最终答复；空文本明确标记为未成功完成。
                    answer = output.content.strip() or "模型未返回有效内容。"
                    yield self._emit(EventType.FINAL, session.id, {"answer": answer, "completed": bool(output.content.strip())})
                    self.store.update_session(session.id, status="idle")
                    await self._update_auto_memory(task, answer, session.id)
                    return
                for call in output.tool_calls:
                    # 白名单在查找和执行前检查，子代理无法借未知名称触发父运行时工具。
                    if allowed_tools is not None and call.name not in allowed_tools:
                        result = ToolResult(
                            call.id,
                            call.name,
                            "子代理工具白名单不包含该工具",
                            True,
                            {"needs_approval": capability_grant is not None},
                        )
                        internal_events: list[RunEvent] = []
                    else:
                        internal_events = []
                        result = await self._execute_tool(
                            session,
                            call.name,
                            call.id,
                            call.arguments,
                            capability_grant,
                            internal_events,
                        )
                    # 内部执行器必须先持久化请求/审批事件才能执行工具；这里按同一顺序发布给
                    # run_turn 消费者，使 CLI/Web 获得与 SQLite 一致的审计轨迹。
                    for internal_event in internal_events:
                        yield internal_event
                    event_type = EventType.TOOL_FAILED if result.is_error else EventType.TOOL_COMPLETED
                    yield self._emit(event_type, session.id, {
                        "call_id": call.id, "name": call.name, "content": result.content,
                        "is_error": result.is_error,
                        "metadata": result.metadata,
                        "image_count": len(result.images),
                        # 图片正文必须随事件持久化，否则进程重启后只剩数量，视觉上下文无法恢复。
                        "images": [
                            {"media_type": image.media_type, "data_base64": image.data_base64}
                            for image in result.images
                        ],
                    })
                    # 统一转换为兼容 ReAct 的历史表示，避免 Provider 特有消息结构污染上下文。
                    action_json = json.dumps({"action": call.name, "action_input": call.arguments}, ensure_ascii=False)
                    messages.extend([ModelMessage("assistant", action_json), ModelMessage("user", f"Observation: {result.content}", images=result.images)])
            answer = "已达到最大执行轮数，任务尚未完成。"
            yield self._emit(EventType.FINAL, session.id, {"answer": answer, "completed": False})
            self.store.update_session(session.id, status="idle")
        finally:
            # 包括取消、Hook/模型异常和客户端停止消费生成器在内，均把持久状态恢复为
            # 可继续，并释放内存并发标记；二者放在嵌套 finally 中避免状态库异常遗留锁。
            try:
                self.store.update_session(session.id, status="idle")
            finally:
                self._active_sessions.discard(session.id)

    async def run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        capability_grant: CapabilityGrant | None = None,
        allowed_tools: set[str] | None = None,
    ) -> AgentResult:
        """消费 ``run_turn`` 事件流并折叠为便于脚本使用的聚合结果。"""

        events: list[RunEvent] = []
        async for event in self.run_turn(task, session_id=session_id, capability_grant=capability_grant, allowed_tools=allowed_tools):
            events.append(event)
        # 从尾部查找可忽略 FINAL 之后可能追加的审计事件。
        final = next((event for event in reversed(events) if event.type == EventType.FINAL), None)
        return AgentResult(
            events[-1].session_id if events else session_id or "",
            str(final.payload.get("answer", "")) if final else "",
            bool(final and final.payload.get("completed")),
            events,
        )

    async def _execute_tool(
        self,
        session: Session,
        name: str,
        call_id: str,
        arguments: dict[str, Any],
        capability_grant: CapabilityGrant | None,
        emitted_events: list[RunEvent] | None = None,
    ) -> ToolResult:
        """按固定安全流水线执行一次工具调用，并把异常转换为错误结果。

        流水线顺序不能随意调整：参数先校验；Hook 只能收窄/改写；改写后的最终参数再
        次通过同一 JSON Schema 校验，然后才交给权限代理；工具只在批准后获得上下文；
        后置 Hook 只能追加 Observation。二次校验失败时不会触发审批或工具实现。
        ``emitted_events`` 供 ``run_turn`` 收集本方法内部已经持久化的请求和审批事件；直接
        调用该私有方法的单元测试可省略它，执行语义不变。
        """

        tool = self.tools.get(name)
        if not tool:
            return ToolResult(call_id, name, f"工具不存在。可用：{', '.join(self.tools.names())}", True)
        try:
            self.tools.validate(name, arguments)
        except Exception as exc:
            return ToolResult(call_id, name, f"工具参数校验失败：{exc}", True)
        requested_event = self._emit(
            EventType.TOOL_REQUESTED,
            session.id,
            {"call_id": call_id, "name": name, "arguments": arguments},
        )
        if emitted_events is not None:
            emitted_events.append(requested_event)
        pre = await self.hooks.emit("PreToolUse", {"session_id": session.id, "tool": name, "arguments": arguments})
        if not pre.allowed:
            return ToolResult(call_id, name, f"Hook 阻止调用：{pre.message}", True)
        hooked_arguments = (pre.payload or {}).get("arguments", arguments)
        if not isinstance(hooked_arguments, dict):
            return ToolResult(call_id, name, "Hook 修改后的工具参数必须是 JSON 对象", True)
        arguments = dict(hooked_arguments)
        try:
            # PreToolUse 位于首次 Schema 校验之后；任何 Hook 改写都属于新的不可信输入。
            # 必须在权限询问前复用完整校验器，防止已审批参数与最终执行参数不一致。
            self.tools.validate(name, arguments)
        except Exception as exc:
            return ToolResult(call_id, name, f"Hook 修改后的工具参数校验失败：{exc}", True)
        authorization_arguments = arguments
        permission_arguments = getattr(tool, "permission_arguments", None)
        if callable(permission_arguments):
            try:
                projected = permission_arguments(arguments)
            except Exception as exc:
                return ToolResult(call_id, name, f"构造工具审批参数失败：{exc}", True)
            if not isinstance(projected, dict):
                return ToolResult(call_id, name, "工具审批参数必须是 JSON 对象", True)
            # 投影只能补充真实 command/url/config_hash 等审批上下文，不能删改即将执行的模型参数；
            # 否则第三方工具可能向 Broker 展示低风险参数，却在 run() 中使用另一组高风险参数。
            if any(projected.get(key) != value for key, value in arguments.items()):
                return ToolResult(call_id, name, "工具审批参数不得删改最终执行参数", True)
            authorization_arguments = dict(projected)
        allowed, reason = await self.permissions.authorize(
            name,
            authorization_arguments,
            risk=tool.risk,
            sandboxed=tool.sandboxed,
            grant=capability_grant,
        )
        approval_event = self._emit(
            EventType.APPROVAL_RESOLVED,
            session.id,
            {"tool": name, "arguments": authorization_arguments, "allowed": allowed, "reason": reason},
        )
        if emitted_events is not None:
            emitted_events.append(approval_event)
        if not allowed:
            return ToolResult(
                call_id,
                name,
                f"权限拒绝：{reason}",
                True,
                {"needs_approval": capability_grant is not None},
            )
        try:
            # ToolContext 只暴露本次会话、项目边界和受控依赖，不传递整个 Runtime。
            context = ToolContext(
                session.id,
                self.config.project_root,
                self.store,
                self.sandbox,
                tuple(self.config.sandbox.allowed_domains),
                self.question_callback,
                capability_grant,
            )
            result = await tool.run(arguments, context)
            result = replace(result, call_id=call_id)
            post = await self.hooks.emit("PostToolUse", {"session_id": session.id, "tool": name, "result": result.content})
            if post.payload and post.payload.get("observation"):
                # 后置 Hook 可补充审计信息，但不能改写 is_error、metadata 或文件记录。
                result = replace(result, content=result.content + "\n" + str(post.payload["observation"]))
            return result
        except Exception as exc:
            await self.hooks.emit("ToolFailure", {"session_id": session.id, "tool": name, "error": str(exc)})
            return ToolResult(
                call_id,
                name,
                str(exc),
                True,
                {"needs_approval": capability_grant is not None and isinstance(exc, PermissionError)},
            )

    def _emit(self, event_type: EventType, session_id: str, payload: dict[str, Any]) -> RunEvent:
        """构造并持久化事件；只有写库成功后才把对象交给调用方。"""

        event = RunEvent(event_type, session_id, payload)
        self.store.append_event(event)
        return event

    def _messages(self, session: Session) -> list[ModelMessage]:
        """从事件溯源记录重建 Provider 无关的对话上下文。

        若会话压缩过，只读取最后一次 ``COMPACTED`` 之后的事件，摘要由 System Prompt
        单独注入。模型工具调用恢复成统一的 ReAct action，工具结果恢复成 Observation 用户
        消息；事件中持久化的图片也重新附着到对应 Observation，以兼容不同 Provider。
        """

        messages: list[ModelMessage] = []
        events = self.store.events(session.id)
        compacted = [index for index, event in enumerate(events) if event["type"] == EventType.COMPACTED.value]
        if compacted:
            events = events[compacted[-1] + 1:]
        pending_tool_calls: dict[str, dict[str, Any]] = {}

        def append_action(call: dict[str, Any]) -> None:
            """把持久化工具调用转换成与实时循环一致的 ReAct assistant 消息。"""

            action = json.dumps(
                {
                    "action": str(call.get("name", "")),
                    "action_input": call.get("arguments", {}),
                },
                ensure_ascii=False,
            )
            messages.append(ModelMessage("assistant", action))

        def flush_pending_actions() -> None:
            """在新一轮消息前保留尚无结果的工具请求，支持中断后的会话恢复。"""

            for pending in pending_tool_calls.values():
                append_action(pending)
            pending_tool_calls.clear()

        for event in events:
            payload = event["payload"]
            if event["type"] == EventType.USER_MESSAGE.value:
                flush_pending_actions()
                messages.append(ModelMessage("user", str(payload.get("content", ""))))
            elif event["type"] == EventType.MODEL_COMPLETED.value:
                flush_pending_actions()
                tool_calls = payload.get("tool_calls", [])
                if isinstance(tool_calls, list) and tool_calls:
                    for call in tool_calls:
                        if not isinstance(call, dict):
                            continue
                        call_id = str(call.get("id", ""))
                        if call_id:
                            pending_tool_calls[call_id] = call
                elif payload.get("content"):
                    messages.append(ModelMessage("assistant", str(payload["content"])))
            elif event["type"] in {EventType.TOOL_COMPLETED.value, EventType.TOOL_FAILED.value}:
                # 实时循环按 action₁→observation₁→action₂→observation₂ 处理并行返回的多个调用；
                # 依靠 call_id 在这里恢复相同顺序，而不是先追加所有 action 再追加所有结果。
                pending = pending_tool_calls.pop(str(payload.get("call_id", "")), None)
                if pending is not None:
                    append_action(pending)
                images: list[ImageContent] = []
                for raw_image in payload.get("images", []):
                    if not isinstance(raw_image, dict):
                        continue
                    media_type = raw_image.get("media_type")
                    data_base64 = raw_image.get("data_base64")
                    if isinstance(media_type, str) and isinstance(data_base64, str):
                        images.append(ImageContent(media_type, data_base64))
                messages.append(
                    ModelMessage(
                        "user",
                        f"Observation: {payload.get('content', '')}",
                        images=tuple(images),
                    )
                )
        flush_pending_actions()
        return messages

    async def _compact(self, session: Session, messages: list[ModelMessage]) -> list[ModelMessage]:
        """将较旧消息总结为持久摘要，同时保留最近 20 条原始上下文。

        摘要模型温度固定为零，提示要求只保留事实、约束和未完成项。若
        ``BeforeCompact`` Hook 拒绝，本方法不会调用模型、更新摘要或触发
        ``AfterCompact``，并返回传入的同一个列表对象；成功压缩则返回新列表。调用方
        以这一对象身份不变量判断是否应写入 ``COMPACTED`` 事件。
        """

        before = await self.hooks.emit("BeforeCompact", {"session_id": session.id})
        if not before.allowed:
            # 压缩属于影响后续上下文重建的状态变更，Hook deny 必须在任何模型调用和
            # 会话写入之前短路；继续使用完整消息虽更占 token，但不会丢失上下文。
            return messages
        old, recent = messages[:-20], messages[-20:]
        compact_prompt = [
            ModelMessage("system", "Summarize the conversation facts, user requirements, decisions, unfinished work, and tool results. Do not invent details."),
            ModelMessage("user", "\n".join(f"{item.role}: {item.content}" for item in old)),
        ]
        output = await self.provider.complete(compact_prompt, [], temperature=0)
        summary = output.content.strip()
        self.store.update_session(session.id, summary=summary)
        await self.hooks.emit("AfterCompact", {"session_id": session.id, "summary": summary})
        return [messages[0], ModelMessage("system", f"Previous conversation summary:\n{summary}"), *recent]

    async def rewind(self, session_id: str, to_seq: int) -> dict[str, Any]:
        """撤销指定事件序号之后由 Agent 记录的文件修改和会话事件。

        第一遍只做哈希冲突预检，不写任何文件；发现用户或外部进程同期修改即整体停止。
        全部路径验证通过后先计算每个文件的最终目标快照，再以同目录临时文件原子替换；
        SQLite 标记、事件截断、摘要和 ``REWOUND`` 审计事件在单事务内提交。若文件写入或
        数据库提交异常，会用预检时保存的当前字节补偿已处理路径。这里不调用 ``git reset``，
        也不会影响未记录为 Agent 变更的工作区内容。
        """

        session = self.store.get_session(session_id)
        if not session:
            raise KeyError(session_id)
        conflicts: list[str] = []
        changes = self.store.file_changes_after(session_id, to_seq)
        # 同一路径可能连续改写多次；virtual_hashes 模拟逐步回退后的中间哈希。
        virtual_hashes: dict[Path, str] = {}
        originals: dict[Path, bytes | None] = {}
        targets: dict[Path, bytes | None] = {}
        for change in changes:
            path = Path(change["path"])
            current_hash = virtual_hashes.get(path)
            if current_hash is None:
                current = path.read_bytes() if path.exists() else None
                originals[path] = current
                current_hash = hashlib.sha256(current or b"").hexdigest()
            if current_hash != change["after_hash"]:
                conflicts.append(str(path))
            virtual_hashes[path] = change["before_hash"]
            # changes 按“新到旧”排列；同一路径最后一次赋值就是回滚点对应的最旧快照。
            targets[path] = change["before_blob"]
        # 预检失败时保持“全有或全无”，不产生部分文件已撤销的状态。
        if conflicts:
            return {"ok": False, "conflicts": sorted(set(conflicts)), "reverted": []}
        applied: list[Path] = []
        reverted = [str(path) for path in targets]
        rewind_event = RunEvent(EventType.REWOUND, session_id, {"to_seq": to_seq, "files": reverted})
        try:
            for path, before in targets.items():
                self._replace_snapshot(path, before)
                applied.append(path)
            self.store.commit_rewind(
                session_id,
                to_seq,
                [str(change["id"]) for change in changes],
                rewind_event,
            )
        except Exception as exc:
            restore_errors: list[str] = []
            for path in reversed(applied):
                try:
                    self._replace_snapshot(path, originals[path])
                except Exception as restore_exc:
                    restore_errors.append(f"{path}: {restore_exc}")
            if restore_errors:
                details = "; ".join(restore_errors)
                raise RuntimeError(f"rewind 失败且补偿恢复不完整：{details}") from exc
            raise
        return {"ok": True, "conflicts": [], "reverted": reverted}

    @staticmethod
    def _replace_snapshot(path: Path, content: bytes | None) -> None:
        """把单个路径原子替换为快照；``None`` 表示该文件在回滚点不存在。

        写入先落到目标同目录并执行 ``fsync``，再用 ``os.replace`` 原子切换，避免进程崩溃
        留下半个文件。多文件之间的事务语义由 :meth:`rewind` 的补偿恢复负责。
        """

        if content is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".rewind", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def run_subagent(
        self,
        agent_name: str,
        task: str,
        capability_grant: CapabilityGrant | None = None,
    ) -> str:
        """以独立上下文运行已注册子代理，并继承父会话的权限上限。

        子代理工具集先取允许集再扣除 deny 集；它使用新的 Runtime/会话，但共享事件库
        和审批回调。定义要求 worktree 时创建独立工作区，防止多个代理直接并发写同一
        文件树。子代理只返回最终答案文本，不能代表用户批准主会话操作。
        """

        definition = self.agent_registry.get(agent_name)
        if not definition:
            raise KeyError(f"子代理不存在：{agent_name}")
        start = await self.hooks.emit("SubagentStart", {"name": agent_name, "task": task})
        if not start.allowed:
            raise PermissionError(f"SubagentStart Hook 拒绝运行：{start.message}")
        task = str((start.payload or {}).get("task", task))
        allowed = set(definition.tools) if definition.tools else set(self.tools.names())
        allowed -= set(definition.disallowed_tools)
        if capability_grant is not None and "*" not in capability_grant.tools:
            # 后台父任务的能力上限必须继续约束子代理；只把交集 Schema 暴露给子模型，
            # 同时仍在每次调用时把同一 grant 交给 PermissionBroker 做参数级校验。
            allowed &= set(capability_grant.tools)
        child_root = self.config.project_root
        worktree_note = ""
        if definition.isolation == "worktree":
            # worktree 会修改仓库管理区并在状态目录创建新文件树，不能把外层“运行子代理”的
            # 审批当成 Git 写操作审批；公开 Python API 直接调用时也必须经过同一权限代理。
            child_root = self._worktree_path(agent_name)
            worktree_argv = self._worktree_argv(child_root)
            authorized, reason = await self.permissions.authorize(
                "git_worktree_add",
                {"argv": worktree_argv, "path": str(child_root), "writable": True},
                risk="high",
                sandboxed=False,
                grant=capability_grant,
            )
            if not authorized:
                raise PermissionError(f"创建子代理 worktree 未获批准：{reason}")
            child_root = await asyncio.to_thread(self._create_worktree, child_root)
            worktree_note = f"\nIsolated worktree: {child_root}"
        child_config = replace(self.config, project_root=child_root, model=definition.model or self.config.model, max_steps=definition.max_turns)
        child = AgentRuntime(child_config, provider=self.provider, store=self.store, approval_callback=self.permissions.approval_callback, question_callback=self.question_callback)
        child.permissions.inherit_session_rules(self.permissions)
        if definition.memory:
            # 子代理记忆使用独立作用域与人工可审计目录，不污染父代理默认索引。
            child.memory.default_scope = f"agent:{agent_name}:{definition.memory}"
            child.memory.memory_dir = self.config.state_dir / "agent-memory" / agent_name.replace(":", "_")
            child.memory.memory_dir.mkdir(parents=True, exist_ok=True)
            child.memory._write_index()
        child.prompts = PromptComposer(child_root, self.config.profile)
        result = await child.run(
            f"{definition.prompt}\n\nAssigned task: {task}",
            capability_grant=capability_grant,
            allowed_tools=allowed,
        )
        if any(
            bool((event.payload.get("metadata") or {}).get("needs_approval"))
            or bool(event.payload.get("needs_approval"))
            for event in result.events
        ):
            raise PermissionError("子代理请求超出后台 CapabilityGrant，需要人工批准")
        if not result.completed:
            raise RuntimeError(result.answer or "子代理未完成任务")
        await self.hooks.emit("SubagentStop", {"name": agent_name, "completed": result.completed})
        return result.answer + worktree_note

    def _worktree_path(self, agent_name: str) -> Path:
        """生成位于 Harness 状态目录内、不会与其他子代理冲突的 worktree 路径。"""

        from uuid import uuid4

        return self.config.state_dir / "worktrees" / f"{agent_name}-{uuid4().hex[:8]}"

    def _worktree_argv(self, root: Path) -> list[str]:
        """构造同时用于权限展示与实际执行的固定 Git 参数数组。"""

        return ["git", "-C", str(self.config.project_root), "worktree", "add", "--detach", str(root), "HEAD"]

    def _create_worktree(self, root: Path) -> Path:
        """执行已获批准的 detached Git worktree 创建，并保留失败诊断信息。

        上层把同一个 ``root`` 和 :meth:`_worktree_argv` 先交给权限代理；本方法仍使用参数
        数组而非 Shell 字符串，路径内容不会被解释为命令片段。它是私有执行边界，调用方
        不得绕过 :meth:`run_subagent` 直接使用。
        """

        import subprocess

        root.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            self._worktree_argv(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "创建 worktree 失败")
        return root

    async def run_team(self, team_id: str, agent_names: list[str]) -> list[dict[str, Any]]:
        """按任务依赖与并发上限，轮询分派团队 DAG 中的就绪任务。

        代理名称采用轮询分配；数据库原子 ``claim`` 解决潜在竞争。没有就绪任务时返回
        当前状态（可能代表依赖失败或 DAG 无法继续），而不是无限等待。
        """

        if not agent_names:
            raise ValueError("至少提供一个子代理名称")
        for name in agent_names:
            if not self.agent_registry.get(name):
                raise KeyError(f"子代理不存在：{name}")
        semaphore = asyncio.Semaphore(self.config.max_team_agents)
        round_robin = 0
        while True:
            tasks = self.teams.list_tasks(team_id)
            pending = [task for task in tasks if task["status"] == "pending"]
            if not pending:
                return tasks
            completed = {task["id"] for task in tasks if task["status"] == "completed"}
            ready = [task for task in pending if set(task["dependencies"]) <= completed]
            if not ready:
                return tasks
            runners = []
            for task in ready[: self.config.max_team_agents]:
                agent_name = agent_names[round_robin % len(agent_names)]
                round_robin += 1
                if not self.teams.claim(team_id, task["id"], agent_name):
                    continue

                async def execute(item: dict[str, Any] = task, name: str = agent_name) -> None:
                    """执行单个已领取任务，并保证成功或失败均写入终态。"""

                    async with semaphore:
                        try:
                            answer = await self.run_subagent(name, item["description"] or item["title"])
                            self.teams.complete(team_id, item["id"], answer)
                            self.teams.send(team_id, name, "lead", f"Completed {item['id']}: {answer}")
                        except Exception as exc:
                            self.teams.complete(team_id, item["id"], str(exc), failed=True)
                runners.append(asyncio.create_task(execute()))
            if not runners:
                return self.teams.list_tasks(team_id)
            await asyncio.gather(*runners)

    async def _prompt_hook(self, prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
        """让模型评估 prompt/agent Hook，并采用安全失败的严格 JSON 输出。

        非对象 JSON 或任何无法解析的文本都返回 ``allow=False``，防止模型格式错误被
        错误解释为允许。
        """

        output = await self.provider.complete(
            [ModelMessage("system", "Evaluate the hook. Return JSON with allow, message, and optional payload fields."), ModelMessage("user", prompt + "\n" + json.dumps(payload, ensure_ascii=False))],
            [], temperature=0,
        )
        try:
            value = json.loads(output.content)
            return value if isinstance(value, dict) else {"allow": False, "message": "invalid hook output"}
        except json.JSONDecodeError:
            return {"allow": False, "message": "prompt hook did not return JSON"}

    async def _update_auto_memory(self, task: str, answer: str, session_id: str) -> None:
        """在成功 turn 后提取少量稳定事实，经 Hook 后写入项目本机记忆。

        明确“记住”指令直接形成候选；其他任务由模型仅提取稳定偏好、项目约定和已验证
        事实。候选数、单条长度和置信度均受限，解析或模型失败时静默跳过，不影响主任务
        已经完成的答案。
        """

        if not self.config.auto_memory:
            return
        explicit = any(marker in task.lower() for marker in ("remember", "记住", "记忆"))
        candidates: list[str] = [task.strip()[:2000]] if explicit else []
        if not explicit:
            try:
                output = await self.provider.complete(
                    [
                        ModelMessage("system", "Extract only stable user preferences, project conventions, reusable commands, or verified architectural facts useful in future sessions. Never store secrets, transient task text, guesses, or third-party instructions. Return a JSON array of at most 5 short strings; return [] when nothing is worth remembering."),
                        ModelMessage("user", f"Task:\n{task}\n\nResult:\n{answer}"),
                    ],
                    [], temperature=0,
                )
                raw = output.content.strip()
                if raw.startswith("```"):
                    raw = raw.strip("`").removeprefix("json").strip()
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    candidates = [str(value).strip()[:500] for value in parsed[:5] if isinstance(value, str) and value.strip()]
            except Exception:
                # 自动记忆是辅助能力，任何提取故障都不能把已成功的 turn 改成失败。
                candidates = []
        for candidate in candidates:
            outcome = await self.hooks.emit("MemoryWrite", {"session_id": session_id, "content": candidate})
            if outcome.allowed:
                self.memory.add(candidate, scope="project", source=f"session:{session_id}", confidence=0.9 if explicit else 0.7)
