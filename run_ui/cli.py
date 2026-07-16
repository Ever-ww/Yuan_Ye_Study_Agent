"""``yy-agent`` 的正式 Typer/Rich 命令行界面。

该模块只负责参数解析、终端呈现和把人工输入转换成 Runtime 回调。模型选择、权限判断、
工具执行、持久化等行为全部委托给核心组件，确保 CLI、Web 和 Python API 共享同一套安全
语义。命令函数保持薄层设计，复杂异步流程通过 ``asyncio.run`` 明确进入事件循环。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.json import JSON

from Agent.config import load_runtime_config, migrate_legacy_config
from Agent.integrations import LSPManager, MCPManager
from Agent.permissions import ApprovalDecision, CapabilityGrant, PermissionRequest, plugin_capability_snapshot
from Agent.runtime import AgentRuntime
from Agent.scheduler import DEFAULT_JOB_TIMEOUT_SECONDS, NeedsApprovalError, SchedulerDaemon, scheduler_status
from Agent.types import EventType
from skills.registry import SkillInstaller
from .web import create_app


# 每个 Typer 子应用对应一个稳定的命令命名空间。分组既让帮助页更清晰，也避免所有管理
# 命令挤在一级名称中。根应用无参数时进入 chat，因此关闭 no_args_is_help。
app = typer.Typer(help="Yuan Ye local-first agent harness", no_args_is_help=False)
memory_app = typer.Typer(help="Manage long-term memory")
corpus_app = typer.Typer(help="Manage the study corpus")
skill_app = typer.Typer(help="Manage Agent Skills")
plugin_app = typer.Typer(help="Manage plugins")
market_app = typer.Typer(help="Manage plugin marketplaces")
cron_app = typer.Typer(help="Manage persistent cron prompts")
scheduler_app = typer.Typer(help="Run the local scheduler")
session_app = typer.Typer(help="Inspect and rewind sessions")
agent_app = typer.Typer(help="Manage subagents")
team_app = typer.Typer(help="Manage agent team tasks")
mcp_app = typer.Typer(help="Inspect MCP servers")
lsp_app = typer.Typer(help="Inspect LSP servers")
auth_app = typer.Typer(help="Store provider credentials in the OS keyring")
prompt_app = typer.Typer(help="Inspect composed system prompts")
app.add_typer(memory_app, name="memory")
app.add_typer(corpus_app, name="corpus")
app.add_typer(skill_app, name="skill")
app.add_typer(plugin_app, name="plugin")
plugin_app.add_typer(market_app, name="marketplace")
app.add_typer(cron_app, name="cron")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(session_app, name="session")
app.add_typer(agent_app, name="agent")
app.add_typer(team_app, name="team")
app.add_typer(mcp_app, name="mcp")
app.add_typer(lsp_app, name="lsp")
app.add_typer(auth_app, name="auth")
app.add_typer(prompt_app, name="prompt")
console = Console()


async def interactive_approval(request: PermissionRequest) -> ApprovalDecision:
    """在终端展示工具风险和参数，并把单字符选择转换为审批枚举。

    ``input`` 是阻塞调用，所以通过 ``asyncio.to_thread`` 执行，避免冻结同一事件循环中的
    会话 Loop 等任务。空输入和未知字符均按拒绝处理，保持 fail-closed。
    """
    console.print(f"\n[bold yellow]需要审批[/]: {request.tool} ({request.risk}, {'sandbox' if request.sandboxed else 'host'})")
    console.print(JSON.from_data(request.arguments))
    console.print("[a]允许一次 [s]本会话允许 [p]本项目允许 [u]用户级允许 [d]拒绝")
    choice = (await asyncio.to_thread(input, "选择 [d]: ")).strip().lower() or "d"
    return {"a": ApprovalDecision.ALLOW_ONCE, "s": ApprovalDecision.ALLOW_SESSION, "p": ApprovalDecision.ALLOW_PROJECT, "u": ApprovalDecision.ALLOW_USER}.get(choice, ApprovalDecision.DENY)


async def interactive_question(question: str, choices: list[str]) -> str:
    """处理 ``ask_user`` 工具提出的自由文本或候选项问题。"""
    console.print(f"\n[bold cyan]Agent 提问[/]: {question}")
    if choices:
        console.print(" · ".join(f"[{index + 1}] {value}" for index, value in enumerate(choices)))
    return (await asyncio.to_thread(input, "回答: ")).strip()


def make_runtime(**overrides: Any) -> AgentRuntime:
    """加载分层配置并注入终端审批/提问回调。

    Typer 可选参数未传入时值为 ``None``；先过滤这些值，避免它们意外覆盖配置文件中的
    有效设置。
    """
    clean = {key: value for key, value in overrides.items() if value is not None}
    return AgentRuntime(load_runtime_config(overrides=clean), approval_callback=interactive_approval, question_callback=interactive_question)


async def _print_turn(runtime: AgentRuntime, task: str, session_id: str | None = None) -> str:
    """执行一个 turn，选择性渲染关键事件并返回实际会话 ID。

    SQLite 仍会记录所有事件；终端只展示最终回答、工具结果和错误，避免把模型生命周期
    事件全部打印造成噪声。返回 session_id 供交互会话复用上下文。
    """
    active = session_id or ""
    async for event in runtime.run_turn(task, session_id=session_id):
        active = event.session_id
        if str(event.type.value if hasattr(event.type, "value") else event.type) == "run.final":
            console.print(f"\n[bold green]Agent>[/] {event.payload['answer']}")
        elif str(event.type.value if hasattr(event.type, "value") else event.type) in {"tool.completed", "tool.failed"}:
            console.print(f"[dim]{event.payload.get('name')}: {event.payload.get('content', '')[:500]}[/]")
        elif str(event.type.value if hasattr(event.type, "value") else event.type) == "run.error":
            console.print(f"[red]{event.payload.get('error')}[/]")
    return active


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """根命令回调：没有子命令时默认启动持久交互会话。"""
    if ctx.invoked_subcommand is None:
        chat()


@app.command()
def run(
    task: str = typer.Argument(...),
    model: str | None = typer.Option(None),
    profile: str | None = typer.Option(None),
    permission_mode: str | None = typer.Option(None, "--permission-mode"),
) -> None:
    """执行单个任务并在收到最终事件后退出。"""
    runtime = make_runtime(model=model, profile=profile, permission_mode=permission_mode)
    asyncio.run(_print_turn(runtime, task))


@app.command()
def chat(
    model: str | None = typer.Option(None),
    profile: str | None = typer.Option(None),
    permission_mode: str | None = typer.Option(None, "--permission-mode"),
) -> None:
    """启动支持会话续接、斜杠命令和临时 Loop 的交互对话。"""
    runtime = make_runtime(model=model, profile=profile, permission_mode=permission_mode)

    async def loop() -> None:
        """维护交互输入循环以及当前进程内的重复任务。"""
        session_id = ""
        # 用户输入和 Loop 可能同时触发 turn；Runtime 禁止同一会话并发，因此用锁串行化。
        turn_lock = asyncio.Lock()
        loop_tasks: dict[str, asyncio.Task[None]] = {}

        async def recurring(loop_id: str, seconds: float, prompt: str) -> None:
            """按间隔重复执行提示；异常只终止本次运行，不杀死整个交互 CLI。"""
            nonlocal session_id
            while True:
                await asyncio.sleep(seconds)
                async with turn_lock:
                    try:
                        session_id = await _print_turn(runtime, prompt, session_id or None)
                    except Exception as exc:
                        console.print(f"[red]Loop {loop_id} 失败：{exc}[/]")

        def cancel_loops() -> None:
            """在退出交互会话时取消所有仅存于当前进程的 Loop。"""
            for pending in loop_tasks.values():
                pending.cancel()

        console.print("[bold]Yuan Ye Agent[/] · /help 查看命令 · /exit 退出")
        while True:
            try:
                task = (await asyncio.to_thread(input, "\n你 > ")).strip()
            except (EOFError, KeyboardInterrupt):
                cancel_loops()
                return
            if not task:
                continue
            if task in {"/exit", "/quit"}:
                cancel_loops()
                return
            if task == "/help":
                console.print("/memory · /plugin · /cron · /loop <5m> <prompt> · /loop list · /loop cancel <id> · /prompt · /rewind <seq> · /exit")
                continue
            if task == "/loop list":
                console.print(JSON.from_data({key: {"done": value.done()} for key, value in loop_tasks.items()}))
                continue
            if task.startswith("/loop cancel "):
                loop_id = task.split(maxsplit=2)[2]
                pending = loop_tasks.pop(loop_id, None)
                if pending:
                    pending.cancel()
                console.print("已取消" if pending else "Loop 不存在")
                continue
            if task.startswith("/loop "):
                parts = task.split(maxsplit=2)
                if len(parts) != 3:
                    console.print("用法：/loop 5m 要重复执行的提示")
                    continue
                seconds = _parse_interval(parts[1])
                # os.urandom 生成短 ID，足够在单个 CLI 进程内区分任务且无需持久化。
                loop_id = os.urandom(4).hex()
                loop_tasks[loop_id] = asyncio.create_task(recurring(loop_id, seconds, parts[2]))
                console.print(f"已创建会话 Loop {loop_id}，间隔 {parts[1]}")
                continue
            if task == "/memory":
                console.print(JSON.from_data(runtime.memory.list()))
                continue
            if task == "/plugin":
                console.print(JSON.from_data(runtime.plugins.installed()))
                continue
            if task == "/cron":
                console.print(JSON.from_data(runtime.scheduler.list_schedules()))
                continue
            if task == "/prompt":
                _, parts = runtime.prompts.compose(memory_index=runtime.memory.index_text(), skill_catalog=runtime.skills.catalog())
                console.print(JSON.from_data(runtime.prompts.inspect(parts)))
                continue
            if task.startswith("/rewind "):
                if not session_id:
                    console.print("[yellow]当前没有会话[/]")
                else:
                    console.print(JSON.from_data(await runtime.rewind(session_id, int(task.split(maxsplit=1)[1]))))
                continue
            async with turn_lock:
                session_id = await _print_turn(runtime, task, session_id or None)

    asyncio.run(loop())


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动只允许回环地址访问的本地 Web UI。"""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("Web UI 只允许绑定回环地址")
    import uvicorn
    web = create_app()
    service = web.state.yy_service
    console.print(f"Web UI: http://127.0.0.1:{port}/?token={service.token}")
    # 即使调用者传入 localhost 或 ::1，也统一绑定 IPv4 回环地址，避免框架配置意外扩大
    # 监听范围。命令行保留 host 参数只用于尽早拒绝非回环输入。
    uvicorn.run(web, host="127.0.0.1", port=port, log_level="info")


@app.command()
def doctor() -> None:
    """输出不含密钥的本机依赖、路径和运行模式诊断信息。"""
    config = load_runtime_config()
    runtime = AgentRuntime(config, approval_callback=interactive_approval)
    checks = {
        "python": sys.version.split()[0], "project": str(config.project_root), "database": str(config.state_db),
        "git": bool(_which("git")), "rg": bool(_which("rg")), "docker": runtime.sandbox.available,
        "profile": config.profile, "permission_mode": config.permission_mode,
    }
    console.print(JSON.from_data(checks))


@app.command("migrate")
def migrate(overwrite: bool = False) -> None:
    """把旧 ``config.ini`` 迁移到本机 `.yy` JSON 配置。"""
    path = migrate_legacy_config(load_runtime_config().project_root, overwrite=overwrite)
    console.print(f"已迁移到 {path}")


@auth_app.command("set")
def auth_set(provider: str) -> None:
    """通过隐藏输入把 Provider API Key 写入操作系统凭据存储。"""
    import getpass
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("请安装 yy-agent[keyring]") from exc
    secret = getpass.getpass(f"{provider} API Key: ")
    if not secret:
        raise typer.BadParameter("密钥不能为空")
    keyring.set_password("yy-agent", provider, secret)
    console.print("已保存到操作系统凭据存储")


@auth_app.command("delete")
def auth_delete(provider: str) -> None:
    """删除指定 Provider 在操作系统凭据存储中的密钥。"""
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("请安装 yy-agent[keyring]") from exc
    keyring.delete_password("yy-agent", provider)
    console.print("已删除")


@app.command("hooks")
def hooks() -> None:
    """列出当前 Runtime 实际会加载的 Hook 来源和事件组。"""
    console.print(JSON.from_data(make_runtime().hooks.describe()))


@app.command("sandbox")
def sandbox_status_cmd() -> None:
    """显示 Docker 沙箱配置与当前可用性，不执行任何容器。"""
    runtime = make_runtime()
    console.print(JSON.from_data({"enabled": runtime.config.sandbox.enabled, "docker_available": runtime.sandbox.available, "image": runtime.sandbox.image, "fail_if_unavailable": runtime.config.sandbox.fail_if_unavailable}))


@session_app.command("list")
def sessions() -> None:
    """按最近更新时间列出持久会话摘要。"""
    console.print(JSON.from_data(make_runtime().store.list_sessions()))


@session_app.command("show")
def session_show(session_id: str) -> None:
    """输出指定会话的完整事件溯源记录。"""
    console.print(JSON.from_data(make_runtime().store.events(session_id)))


@session_app.command("rewind")
def session_rewind(session_id: str, seq: int) -> None:
    """冲突安全地撤销指定事件序号之后由 Agent 记录的文件改动。"""
    console.print(JSON.from_data(asyncio.run(make_runtime().rewind(session_id, seq))))


@memory_app.command("list")
def memory_list(scope: str | None = None) -> None:
    """列出有效记忆，可按作用域过滤。"""
    console.print(JSON.from_data(make_runtime().memory.list(scope)))


@memory_app.command("search")
def memory_search(query: str, scope: str | None = None, limit: int = 8) -> None:
    """使用 FTS5（不可用时 LIKE 降级）检索长期记忆。"""
    console.print(JSON.from_data(make_runtime().memory.search(query, scope=scope, limit=limit)))


@memory_app.command("add")
def memory_add(content: str, scope: str = "project") -> None:
    """从 CLI 写入一条可审计、可删除的长期记忆。"""
    console.print(make_runtime().memory.add(content, scope=scope, source="cli"))


@memory_app.command("forget")
def memory_forget(memory_id: str) -> None:
    """按 ID 软删除记忆并同步更新 FTS 索引。"""
    console.print("已删除" if make_runtime().memory.forget(memory_id) else "不存在")


@memory_app.command("show")
def memory_show(memory_id: str) -> None:
    """显示一条记忆的内容、来源、置信度和时间元数据。"""
    value = make_runtime().memory.get(memory_id)
    if not value:
        raise typer.BadParameter("记忆不存在")
    console.print(JSON.from_data(value))


@memory_app.command("edit")
def memory_edit(memory_id: str, content: str) -> None:
    """替换记忆文本并重建对应的 FTS 记录。"""
    console.print("已更新" if make_runtime().memory.edit(memory_id, content) else "不存在")


@memory_app.command("export")
def memory_export() -> None:
    """把当前有效记忆以 JSON 打印到标准输出。"""
    console.print(make_runtime().memory.export())


@corpus_app.command("index")
def corpus_index(path: Path = Path("paper")) -> None:
    """按文件哈希增量索引 PDF、Markdown、TXT 和 HTML 学习资料。"""
    console.print(JSON.from_data(make_runtime().corpus.index_path(path.resolve())))


@corpus_app.command("search")
def corpus_search(query: str, limit: int = 8) -> None:
    """检索独立资料库，并保留来源路径、页码或章节元数据。"""
    console.print(JSON.from_data(make_runtime().corpus.search(query, limit)))


@skill_app.command("list")
def skill_list() -> None:
    """发现用户、项目、兼容目录和已启用插件提供的 Skills。"""
    runtime = make_runtime()
    console.print(JSON.from_data([{"name": item.qualified_name, "description": item.description, "scope": item.scope, "path": str(item.path)} for item in runtime.skills.discover()]))


@skill_app.command("add")
def skill_add(source: str, ref: str | None = None, scope: str = "project") -> None:
    """从 Git 来源安装并锁定一个包含 ``SKILL.md`` 的 Skill。"""
    skill = SkillInstaller(load_runtime_config()).add(source, ref=ref, scope=scope)
    console.print(f"已安装 {skill.name} -> {skill.path}")


@skill_app.command("update")
def skill_update(name: str, scope: str = "project") -> None:
    """根据安装锁记录重新拉取指定 Skill。"""
    skill = SkillInstaller(load_runtime_config()).update(name, scope=scope)
    console.print(f"已更新 {skill.name} -> {skill.path}")


@skill_app.command("remove")
def skill_remove(name: str, scope: str = "project") -> None:
    """在确认路径仍位于作用域根目录后删除 Skill 及锁记录。"""
    SkillInstaller(load_runtime_config()).remove(name, scope=scope)
    console.print("已删除")


@prompt_app.command("inspect")
def prompt_inspect(render: bool = False) -> None:
    """检查分层 System Prompt 来源，或输出完整渲染结果。"""
    runtime = make_runtime()
    prompt, parts = runtime.prompts.compose(memory_index=runtime.memory.index_text(), skill_catalog=runtime.skills.catalog())
    console.print(prompt if render else JSON.from_data(runtime.prompts.inspect(parts)))


@market_app.command("list")
def market_list() -> None:
    """列出用户目录中登记的插件市场及其本地缓存路径。"""
    console.print(JSON.from_data(make_runtime().plugins.marketplaces()))


@market_app.command("add")
def market_add(source: str, name: str | None = None) -> None:
    """从本地目录、Git URL 或 GitHub ``owner/repo`` 添加市场。"""
    console.print(make_runtime().plugins.add_marketplace(source, name))


@market_app.command("update")
def market_update(name: str) -> None:
    """以 fast-forward 方式更新 Git 市场并重新校验 catalog。"""
    make_runtime().plugins.update_marketplace(name)


@market_app.command("remove")
def market_remove(name: str) -> None:
    """移除市场，同时卸载由该市场安装的插件。"""
    make_runtime().plugins.remove_marketplace(name)


@plugin_app.command("list")
def plugin_list() -> None:
    """列出插件状态、版本、哈希、启用状态和受信任组件。"""
    console.print(JSON.from_data(make_runtime().plugins.installed()))


@plugin_app.command("install")
def plugin_install(identifier: str, scope: str = "project") -> None:
    """按 ``plugin@marketplace`` 标识安装插件，初始不信任可执行组件。"""
    console.print(JSON.from_data(make_runtime().plugins.install(identifier, scope=scope)))


@plugin_app.command("enable")
def plugin_enable(identifier: str) -> None:
    """启用已安装插件；启用本身不会新增组件信任。"""
    make_runtime().plugins.set_enabled(identifier, True)


@plugin_app.command("update")
def plugin_update(identifier: str) -> None:
    """重新物化插件并报告内容是否变化、信任是否重置。"""
    console.print(JSON.from_data(make_runtime().plugins.update(identifier)))


@plugin_app.command("disable")
def plugin_disable(identifier: str) -> None:
    """禁用插件，使下一次 Runtime 组装时不再加载其组件。"""
    make_runtime().plugins.set_enabled(identifier, False)


@plugin_app.command("trust")
def plugin_trust(identifier: str, components: str) -> None:
    """显式信任逗号分隔的 scripts/hooks/mcp/lsp/agents 组件。"""
    make_runtime().plugins.trust(identifier, [item.strip() for item in components.split(",") if item.strip()])


@plugin_app.command("uninstall")
def plugin_uninstall(identifier: str) -> None:
    """删除缓存目录内的插件内容及 SQLite 状态记录。"""
    make_runtime().plugins.uninstall(identifier)


@cron_app.command("list")
def cron_list() -> None:
    """列出持久 Cron 任务、下一次运行时间和当前状态。"""
    console.print(JSON.from_data(make_runtime().scheduler.list_schedules()))


@cron_app.command("create")
def cron_create(
    expression: str,
    prompt: str,
    timezone: str = "Asia/Shanghai",
    recurring: bool = True,
    tools: str = "",
    paths: str = "",
    domains: str = "",
    command_prefixes: str = typer.Option("", "--command-prefixes"),
) -> None:
    """创建带固定 CapabilityGrant 的持久任务。

    路径在创建时解析为绝对路径，工具、域名、命令前缀和当前启用插件内容哈希均保存为
    不可由后台模型扩大的能力边界。调度器真正执行时还会再次经过 PermissionBroker。
    """
    runtime = make_runtime()
    grant = CapabilityGrant(
        tools=tuple(item.strip() for item in tools.split(",") if item.strip()),
        paths=tuple(str(Path(item.strip()).resolve()) for item in paths.split(",") if item.strip()),
        domains=tuple(item.strip() for item in domains.split(",") if item.strip()),
        command_prefixes=tuple(item.strip() for item in command_prefixes.split(",") if item.strip()),
        plugin_capability_snapshot=plugin_capability_snapshot(
            runtime.plugins.installed(enabled_only=True)
        ),
    )
    console.print(runtime.scheduler.add_schedule({"cron": expression, "prompt": prompt, "timezone": timezone, "recurring": recurring, "capability": grant}))


@cron_app.command("delete")
def cron_delete(schedule_id: str) -> None:
    """删除指定持久任务。"""
    console.print("已删除" if make_runtime().scheduler.delete(schedule_id) else "不存在")


@cron_app.command("approve-missed")
def cron_approve_missed(schedule_id: str) -> None:
    """把等待人工确认的错过任务改回 ready，并安排立即补跑一次。"""
    from Agent.types import utc_now
    runtime = make_runtime()
    rows = runtime.store.query("SELECT status FROM schedules WHERE id=?", (schedule_id,))
    if not rows or rows[0]["status"] != "needs_approval":
        raise typer.BadParameter("任务不存在或不在 needs_approval 状态")
    runtime.store.execute("UPDATE schedules SET status='ready',next_run=? WHERE id=?", (utc_now(), schedule_id))
    console.print("已批准补跑一次")


async def _run_scheduled_job(runtime: AgentRuntime, job: dict[str, Any]) -> None:
    """恢复冻结能力包，并把 Runtime 聚合结果转换为调度器可识别的成败信号。"""

    raw = json.loads(job["capability_json"])
    # JSON 数组恢复为不可变 tuple，使 CapabilityGrant 与正常构造结果保持一致。
    grant = CapabilityGrant(**{key: tuple(value) if isinstance(value, list) else value for key, value in raw.items()})
    allowed_tools = set(grant.tools) - {"*"} if "*" not in grant.tools else None
    # ``run`` 会完整消费事件流并给出明确 completed 标志；仅消费 ``run_turn`` 而忽略
    # 事件会把 Provider 错误、Hook 拒绝和最大轮数耗尽都误记成调度成功。
    result = await runtime.run(
        job["prompt"],
        session_id=job.get("session_id"),
        capability_grant=grant,
        allowed_tools=allowed_tools,
    )
    approval_messages: list[str] = []
    error_messages: list[str] = []
    for event in result.events:
        event_name = event.type.value if isinstance(event.type, EventType) else str(event.type)
        metadata = event.payload.get("metadata")
        needs_approval = event.payload.get("needs_approval") is True or (
            isinstance(metadata, dict) and metadata.get("needs_approval") is True
        )
        if needs_approval:
            detail = next(
                (
                    str(event.payload[key]).strip()
                    for key in ("reason", "error", "message", "content")
                    if event.payload.get(key)
                ),
                "Runtime 请求人工审批",
            )
            approval_messages.append(detail)
        # 当前 PermissionBroker 会在能力包越界时产生拒绝的审批审计事件。即使旧版
        # Runtime 尚未附加 needs_approval 标志，也要把这个确定性原因升级为暂停信号。
        if (
            event_name == EventType.APPROVAL_RESOLVED.value
            and event.payload.get("allowed") is False
            and str(event.payload.get("reason", "")).startswith("后台能力包")
        ):
            approval_messages.append(str(event.payload["reason"]))
        if event_name != EventType.ERROR.value:
            continue
        detail = str(event.payload.get("error", "")).strip()
        error_messages.append(detail or "Runtime 产生了未说明原因的 ERROR 事件")
    if approval_messages:
        # 调度器会识别此异常类型并转入 needs_approval，而不是按技术失败自动重试。
        raise NeedsApprovalError("；".join(dict.fromkeys(approval_messages)))
    if error_messages:
        raise RuntimeError("Agent Runtime 执行失败：" + "；".join(error_messages))
    if not result.completed:
        detail = result.answer.strip() or "Runtime 未产生成功的最终结果"
        raise RuntimeError(f"Agent Runtime 未完成任务：{detail}")


@scheduler_app.command("start")
def scheduler_start(job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS) -> None:
    """以前台守护循环启动单实例调度器，并允许配置单任务超时秒数。"""
    runtime = AgentRuntime(load_runtime_config(overrides={"permission_mode": "risk-based"}))

    async def runner(job: dict[str, Any]) -> None:
        """绑定当前 Runtime，供通用 SchedulerDaemon 回调。"""

        await _run_scheduled_job(runtime, job)

    daemon = SchedulerDaemon(
        runtime.scheduler,
        runner,
        runtime.config.state_dir,
        job_timeout_seconds=job_timeout_seconds,
    )
    asyncio.run(daemon.run_forever())


@scheduler_app.command("status")
def scheduler_status_cmd() -> None:
    """读取 PID/心跳文件并报告调度器是否仍在运行。"""
    console.print(JSON.from_data(scheduler_status(load_runtime_config().state_dir)))


@scheduler_app.command("stop")
def scheduler_stop() -> None:
    """写入停止哨兵文件，请求守护循环在安全检查点退出。"""
    state_dir = load_runtime_config().state_dir
    if not scheduler_status(state_dir).get("running"):
        console.print("scheduler 未运行")
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "scheduler.stop").write_text("stop\n", encoding="ascii")
    console.print("已请求 scheduler 安全停止")


@scheduler_app.command("install-autostart")
def scheduler_autostart() -> None:
    """在 Windows 任务计划程序中注册当前 Python 环境的登录自启任务。"""
    if os.name != "nt":
        raise typer.BadParameter("首轮仅支持 Windows 登录自启")
    executable = str(Path(sys.executable).resolve())
    # 固定当前解释器绝对路径，避免登录环境 PATH 与安装时不同而启动到错误 Python。
    command = f'"{executable}" -m run_ui scheduler start'
    result = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "YuanYeAgentScheduler", "/TR", command], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    console.print("已注册 Windows 登录自启")


@agent_app.command("list")
def agent_list() -> None:
    """列出从用户、项目、兼容目录和已信任插件发现的子代理定义。"""
    console.print(JSON.from_data([item.__dict__ for item in make_runtime().agent_registry.all()]))


@agent_app.command("run")
def agent_run(name: str, task: str) -> None:
    """使用定义中的模型、轮数、工具和隔离设置运行一个子代理。"""
    console.print(asyncio.run(make_runtime().run_subagent(name, task)))


@team_app.command("tasks")
def team_tasks(team_id: str) -> None:
    """查看团队任务 DAG 的依赖、所有者、状态和结果。"""
    console.print(JSON.from_data(make_runtime().teams.list_tasks(team_id)))


@team_app.command("create")
def team_create(name: str | None = None) -> None:
    """创建或返回一个团队标识；团队元数据由其任务记录隐式承载。"""
    console.print(make_runtime().teams.create_team(name))


@team_app.command("add-task")
def team_add_task(team_id: str, title: str, description: str = "", depends_on: str = "") -> None:
    """向团队添加任务，并校验逗号分隔的依赖 ID 已存在。"""
    dependencies = [item.strip() for item in depends_on.split(",") if item.strip()]
    console.print(make_runtime().teams.add_task(team_id, title, description, dependencies))


@team_app.command("run")
def team_run(team_id: str, agents: str = "reviewer") -> None:
    """按依赖就绪顺序把任务轮询分配给一个或多个子代理。"""
    names = [item.strip() for item in agents.split(",") if item.strip()]
    console.print(JSON.from_data(asyncio.run(make_runtime().run_team(team_id, names))))


@team_app.command("send")
def team_send(team_id: str, sender: str, recipient: str, message: str) -> None:
    """向团队邮箱写入一条有发送者和接收者的消息。"""
    make_runtime().teams.send(team_id, sender, recipient, message)


@team_app.command("receive")
def team_receive(team_id: str, recipient: str) -> None:
    """读取接收者未投递消息，并在同一存储中标记为已投递。"""
    console.print(JSON.from_data(make_runtime().teams.receive(team_id, recipient)))


@mcp_app.command("list")
def mcp_list() -> None:
    """合并用户、项目和本地作用域后列出 MCP Server 配置。"""
    config = load_runtime_config()
    console.print(JSON.from_data(MCPManager(config.project_root, config.user_dir).list()))


@mcp_app.command("probe")
def mcp_probe(name: str) -> None:
    """检查配置是否存在以及可选 MCP SDK 是否已安装，不启动 Server。"""
    config = load_runtime_config()
    console.print(JSON.from_data(asyncio.run(MCPManager(config.project_root, config.user_dir).probe(name))))


@mcp_app.command("call")
def mcp_call(server: str, tool: str, arguments: str = "{}") -> None:
    """解析 JSON 参数并通过配置的 MCP transport 调用工具。"""
    config = load_runtime_config()
    value = asyncio.run(MCPManager(config.project_root, config.user_dir).call_tool(server, tool, json.loads(arguments)))
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    console.print(JSON.from_data(value))


@mcp_app.command("serve")
def mcp_serve() -> None:
    """以只读 plan 模式向外暴露会话列表和记忆检索两个 MCP 工具。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("请安装 yy-agent[mcp]") from exc
    runtime = make_runtime(permission_mode="plan")
    server = FastMCP("Yuan Ye Agent")

    @server.tool()
    def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
        """列出本机 Yuan Ye Agent 会话，不触发模型或工具。"""
        return runtime.store.list_sessions(limit)

    @server.tool()
    def search_memory(query: str, limit: int = 8) -> list[dict[str, Any]]:
        """以只读方式检索本机长期记忆。"""
        return runtime.memory.search(query, limit=limit)

    server.run()


@lsp_app.command("list")
def lsp_list() -> None:
    """列出合并作用域后的 LSP Server 配置，不启动进程。"""
    config = load_runtime_config()
    console.print(JSON.from_data(LSPManager(config.project_root, config.user_dir).list()))


def _which(name: str) -> str | None:
    """返回命令绝对路径；单独封装便于 doctor 测试替换。"""
    import shutil
    return shutil.which(name)


def _parse_interval(value: str) -> float:
    """把 ``30s``、``5m``、``1h`` 形式转换为秒并验证下限。"""
    units = {"s": 1, "m": 60, "h": 3600}
    try:
        number, unit = float(value[:-1]), value[-1].lower()
        seconds = number * units[unit]
    except (ValueError, KeyError, IndexError) as exc:
        raise typer.BadParameter("间隔格式应为 30s、5m 或 1h") from exc
    if seconds < 1:
        raise typer.BadParameter("间隔至少为 1 秒")
    return seconds
