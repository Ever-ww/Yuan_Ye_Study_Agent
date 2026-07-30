"""实时 Rich CLI：生产命令只消费 Gateway 事件。"""

from __future__ import annotations

import asyncio
import shlex
import signal
import sys
from pathlib import Path
from types import FrameType

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from Agent import (
    AgentRuntime,
    EventType,
    ModelRetryPolicy,
    RuntimeFailure,
    default_agent_root,
    load_runtime_config,
)
from bootstrap import ensure_project_initialized, initialize_project
from memory import MemoryStore
from gateway import GatewayClient, GatewayProcessManager
from gateway.models import GatewayEventEnvelope
from skill import SkillInstallRequest
from .approval import InteractiveApproval, active_live as _active_live
from .harness_loader import load_harness_module
from .web import serve

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Yuan Ye Study Agent 本地入口")
session_app = typer.Typer(help="列出、查看和恢复本地会话")
gateway_app = typer.Typer(help="管理本机 Gateway 后台进程")
app.add_typer(session_app, name="session")
app.add_typer(gateway_app, name="gateway")
console = Console()


class ChatInterruptController:
    """把 Ctrl+C 路由到当前回答；空闲时保留终端退出语义。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_task: asyncio.Task[object] | None = None
        self._cancel_requested = False

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_active(self, task: asyncio.Task[object]) -> None:
        self._active_task = task
        self._cancel_requested = False

    def clear_active(self) -> None:
        self._active_task = None

    def consume_cancel_request(self) -> bool:
        requested = self._cancel_requested
        self._cancel_requested = False
        return requested

    def handle_sigint(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        task = self._active_task
        loop = self._loop
        if task is not None and not task.done() and loop is not None:
            self._cancel_requested = True
            loop.call_soon_threadsafe(task.cancel)
            return
        raise KeyboardInterrupt


@app.command()
def init() -> None:
    """初始化本机 `.yy` 配置、会话索引和长期记忆文件。"""
    yy = initialize_project(default_agent_root())
    console.print(f"[green]初始化完成[/] {yy}")
    console.print(f"请编辑 {yy / 'settings.local.json'} 配置模型；已有文件不会被覆盖。")


def _memory() -> MemoryStore:
    """从当前项目配置创建 Memory 门面。"""
    config = load_runtime_config()
    return MemoryStore(
        config.memory_dir,
        workspace_root=config.workspace_root,
        agent_root=config.agent_root,
    )


def _gateway_client(*, port: int | None = None) -> GatewayClient:
    """发现或自动启动单实例 Gateway。"""
    config = load_runtime_config()
    return GatewayClient(
        config.agent_root,
        port=port or config.gateway_port,
    )


async def _gateway_project(client: GatewayClient) -> dict[str, object]:
    """把当前启动目录注册为 Gateway 项目。"""
    return await client.register_project(Path.cwd())


async def _render_gateway(
    client: GatewayClient,
    project_id: str,
    task: str,
    session_id: str | None = None,
) -> tuple[str, str]:
    """消费 Gateway 可重放事件，并把审批决定发回原 Run。"""
    run = await client.start_run(project_id, task, session_id)
    lines: list[str] = []
    streaming_text = ""
    active_session_id = session_id or ""
    terminal_error = ""
    with Live(Panel("正在排队…", title="Yuan Ye Gateway"), console=console, refresh_per_second=10) as live:
        token = _active_live.set(live)
        try:
            async for event in client.subscribe(run.run_id):
                if event.session_id:
                    active_session_id = event.session_id
                if event.type == EventType.TEXT.value:
                    streaming_text += str(event.payload.get("content", ""))
                elif event.type == EventType.MODEL_RETRY.value:
                    if streaming_text:
                        lines.append("[yellow]网络中断前的不完整流式片段已丢弃[/]")
                        streaming_text = ""
                    lines.append(
                        f"[yellow]模型网络异常，{event.payload.get('delay_seconds', 2)} 秒后重试[/]"
                    )
                elif event.type == EventType.MODEL_RECONNECTED.value:
                    lines.append("[green]模型网络连接已恢复[/]")
                elif event.type == EventType.TOOL_REQUESTED.value:
                    if streaming_text:
                        lines.append(streaming_text)
                        streaming_text = ""
                    lines.append(f"[cyan]工具请求[/] {event.payload.get('name', '')}")
                elif event.type == EventType.TOOL_COMPLETED.value:
                    lines.append(f"[green]工具完成[/] {event.payload.get('name', '')}")
                elif event.type == "approval_requested":
                    approved = await InteractiveApproval(console)(
                        str(event.payload.get("tool_name", "")),
                        dict(event.payload.get("arguments", {})),
                    )
                    await client.respond_approval(
                        str(event.payload["approval_id"]),
                        approved,
                    )
                elif event.type == EventType.COMPRESSION_STARTED.value:
                    lines.append("[cyan]正在压缩上下文…[/]")
                elif event.type == EventType.CONTEXT_COMPRESSED.value:
                    lines.append(f"[green]{event.payload.get('message', '上下文压缩完成')}[/]")
                elif event.type == EventType.COMPRESSION_FALLBACK.value:
                    lines.append(f"[yellow]{event.payload.get('message', '上下文裁剪降级')}[/]")
                elif event.type in {"run_failed", "run_cancelled", "run_interrupted"}:
                    terminal_error = str(event.payload.get("message", "运行结束"))
                    lines.append(f"[red]{terminal_error}[/]")
                elif event.type == "run_completed":
                    answer = str(event.payload.get("answer", ""))
                    if answer and not streaming_text:
                        lines.append(f"[bold green]{answer}[/]")
                display = lines[-12:] + ([streaming_text] if streaming_text else [])
                live.update(Panel("\n".join(display) or "正在运行…", title="Yuan Ye Gateway"))
                if event.type in {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}:
                    break
        except asyncio.CancelledError:
            await client.cancel_run(run.run_id)
            raise
        finally:
            _active_live.reset(token)
    return active_session_id, terminal_error


async def _approve(name: str, arguments: dict[str, object]) -> bool:
    """兼容测试和外部调用的一次性审批入口。"""
    return await InteractiveApproval(console)(name, arguments)


async def _render(
    runtime: AgentRuntime,
    task: str,
    session_id: str | None = None,
    *,
    propagate_errors: bool = False,
) -> str:
    """边接收事件边刷新面板，避免模型等待期间终端静止。"""
    lines: list[str] = []
    streaming_text = ""
    displayed_status = ""
    active_session_id = session_id or ""
    try:
        with Live(Panel("正在准备…", title="Yuan Ye Agent"), console=console, refresh_per_second=10) as live:
            token = _active_live.set(live)
            try:
                async for event in runtime.run_task(task, session_id):
                    if event.type is EventType.STARTED:
                        active_session_id = str(event.payload["session_id"])
                    elif event.type is EventType.TEXT:
                        streaming_text += str(event.payload["content"])
                    elif event.type is EventType.MODEL_RETRY:
                        if streaming_text:
                            lines.append("[yellow]本次流式片段因网络中断已丢弃[/]")
                            streaming_text = ""
                        lines.append(
                            f"[yellow]模型网络异常，正在等待重连；{event.payload['delay_seconds']} 秒后进行 "
                            f"第 {event.payload['attempt']}/{event.payload['max_attempts']} 次请求[/]"
                        )
                    elif event.type is EventType.MODEL_RECONNECTED:
                        lines.append("[green]模型网络连接已恢复，继续当前任务[/]")
                    elif event.type is EventType.TOOL_REQUESTED:
                        if streaming_text:
                            lines.append(streaming_text)
                            streaming_text = ""
                        lines.append(f"[cyan]工具请求[/] {event.payload['name']}")
                    elif event.type is EventType.TOOL_COMPLETED:
                        lines.append(f"[green]工具完成[/] {event.payload['name']}")
                    elif event.type is EventType.COMPRESSION_STARTED:
                        lines.append("[cyan]正在压缩上下文…[/]")
                    elif event.type is EventType.CONTEXT_COMPRESSED:
                        displayed_status = str(event.payload.get("message", "上下文压缩完成"))
                        lines.append(f"[green]{displayed_status}[/]")
                    elif event.type is EventType.COMPRESSION_FALLBACK:
                        displayed_status = str(event.payload.get("message", "压缩失败，已启用内存裁剪"))
                        lines.append(f"[yellow]{displayed_status}[/]")
                    elif event.type is EventType.ERROR:
                        lines.append(f"[red]错误[/] {event.payload['message']}")
                    elif event.type is EventType.FINAL:
                        answer = str(event.payload["answer"])
                        if answer and answer != displayed_status and not streaming_text and (not lines or answer != lines[-1]):
                            lines.append(f"[bold green]{answer}[/]")
                    display = lines[-12:] + ([streaming_text] if streaming_text else [])
                    live.update(Panel("\n".join(display) or "正在思考…", title="Yuan Ye Agent"))
            finally:
                _active_live.reset(token)
    except Exception as exc:
        if propagate_errors:
            raise
        console.print(Panel(f"[red]{str(exc) or type(exc).__name__}[/]", title="Yuan Ye Agent 运行错误"))
    return active_session_id


@app.command()
def run(task: str, session_id: str | None = typer.Option(None, "--session", "-s", help="继续指定会话哈希")) -> None:
    """通过本机 Gateway 运行一次任务。"""
    try:
        async def execute() -> str:
            client = _gateway_client()
            project = await _gateway_project(client)
            active_id, _ = await _render_gateway(
                client,
                str(project["project_id"]),
                task,
                session_id,
            )
            return active_id

        active_id = asyncio.run(execute())
    except Exception as exc:
        console.print(Panel(f"[red]{str(exc) or type(exc).__name__}[/]", title="Yuan Ye Gateway 错误"))
        return
    if active_id:
        console.print(f"[dim]会话哈希：{active_id}[/]")


@app.command()
def chat(session_id: str | None = typer.Option(None, "--session", "-s", help="恢复指定会话哈希")) -> None:
    """连接 Gateway 并启动连续交互会话。"""
    interrupts = ChatInterruptController()
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, interrupts.handle_sigint)
    try:
        asyncio.run(_chat_gateway(session_id, interrupt_controller=interrupts))
    except KeyboardInterrupt:
        console.print("\n[dim]已退出会话。[/]")
    except Exception as exc:
        console.print(Panel(f"[red]{str(exc) or type(exc).__name__}[/]", title="Yuan Ye Agent 配置错误"))
        raise typer.Exit(code=1) from exc
    finally:
        signal.signal(signal.SIGINT, previous_handler)


async def _chat_gateway(
    session_id: str | None,
    *,
    interrupt_controller: ChatInterruptController,
) -> None:
    console.print(
        "[bold cyan]Yuan Ye Gateway[/]  输入 /help 查看命令，/exit 退出；"
        "运行中按 Ctrl+C 终止当前回答。"
    )
    client = _gateway_client()
    project = await _gateway_project(client)
    project_id = str(project["project_id"])
    if session_id:
        sessions = await client.sessions(project_id)
        if not any(item.get("session_id") == session_id for item in sessions):
            raise ValueError(f"当前 workspace 未找到 Session：{session_id}")
        console.print(f"[green]已恢复会话[/] {session_id}")
    interrupt_controller.bind(asyncio.get_running_loop())
    unread = await client.inbox(unread_only=True)
    if unread:
        console.print(f"[yellow]Inbox 有 {len(unread)} 条未读后台结果。[/]")
    while True:
        try:
            task = console.input("[bold blue]你 > [/]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]已退出客户端；Gateway 中的后台任务会继续运行。[/]")
            return
        if task in {"/exit", "/quit"}:
            return
        if task == "/help":
            console.print(
                "/code 进入 Hook Extension Coding 模式；"
                "/compress；/context refresh；/skill list|install|update|audit|refresh；/exit；"
                "运行中 Ctrl+C 取消当前 Run，空闲时 Ctrl+C 退出客户端。"
            )
            continue
        if task == "/code":
            await _code_mode(client, project_id)
            continue
        if task == "/skill" or task.startswith("/skill "):
            await _handle_gateway_skill_command(client, project_id, task)
            continue
        if not task:
            continue
        previous_id = session_id
        render_task = asyncio.create_task(
            _render_gateway(client, project_id, task, session_id),
        )
        interrupt_controller.set_active(render_task)
        try:
            session_id, _ = await render_task
        except asyncio.CancelledError:
            if not interrupt_controller.consume_cancel_request():
                raise
            console.print("[yellow]已请求 Gateway 终止当前回答，可继续输入。[/]")
        finally:
            interrupt_controller.clear_active()
        if session_id and not previous_id:
            console.print(f"[dim]会话哈希：{session_id}[/]")


async def _code_mode(client: GatewayClient, project_id: str) -> None:
    """在 Gateway 托管的隔离 worktree 中运行持续 Extension Coding 会话。"""
    try:
        with console.status("[cyan]正在创建隔离 Git worktree 和 Coding Runtime…[/]"):
            session = await client.start_code_session(project_id)
    except Exception as exc:
        console.print(Panel(
            str(exc) or type(exc).__name__,
            title="无法进入 /code",
            border_style="red",
        ))
        return
    console.print(
        Panel(
            f"worktree: {session.worktree_path}\nbranch: {session.branch}\n"
            "每条需求都会生成独立测试并完成回归验证。输入 /exit 合并并返回聊天，"
            "输入 /abort 放弃全部改动。",
            title="Extension Coding 模式",
            border_style="cyan",
        )
    )
    while True:
        try:
            task = console.input("[bold magenta]Code > [/]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print(
                "\n[yellow]Coding Session 已保留，未自动合并。"
                "Gateway 重启也不会自动合并，请检查上方 worktree。[/]"
            )
            return
        if not task:
            continue
        if task in {"/exit", "/quit"}:
            try:
                with console.status("[cyan]正在检查并 fast-forward 合并…[/]"):
                    result = await client.finalize_code_session(session.code_session_id)
            except Exception as exc:
                console.print(f"[red]{str(exc) or type(exc).__name__}[/]")
                continue
            style = "green" if result.merged or result.status == "no_changes" else "yellow"
            console.print(f"[{style}]{result.message}[/]")
            if result.stay_in_code_mode:
                continue
            if result.worktree_path:
                console.print(
                    f"[dim]保留 worktree：{result.worktree_path}\n分支：{result.branch}[/]"
                )
            return
        if task == "/abort":
            if not typer.confirm("放弃当前 Coding Session 的全部隔离改动？", default=False):
                continue
            result = await client.abort_code_session(session.code_session_id)
            console.print(f"[yellow]{result.message}[/]")
            return
        try:
            result = await _run_code_turn_with_progress(
                client, session.code_session_id, task,
            )
            style = "green" if result.status == "verified" else "red"
            console.print(Panel(
                f"{result.message}\n测试文件：{result.test_file}\n"
                f"尝试次数：{result.attempts}"
                + (f"\n临时提交：{result.commit}" if result.commit else "")
                + (f"\n\nAgent：{result.diagnostic}" if result.diagnostic else ""),
                title="Coding 验证结果",
                border_style=style,
            ))
        except Exception as exc:
            console.print(Panel(
                str(exc) or type(exc).__name__,
                title="Coding Turn 失败",
                border_style="red",
            ))


async def _run_code_turn_with_progress(
    client: GatewayClient,
    session_id: str,
    task: str,
):
    """轮询持久化 Coding 事件，使长时间生成和测试不会表现为静止。"""
    pending = asyncio.create_task(client.run_code_turn(session_id, task))
    sequence = 0
    labels = {
        "code_turn_started": "正在分析需求并分配唯一测试文件…",
        "code_generation": "Coding Agent 正在生成扩展代码…",
        "code_auto_repair": "测试未通过，Coding Agent 正在自动修复…",
        "code_test": "控制器正在执行验证命令…",
        "code_turn_verified": "验证通过，正在创建临时提交…",
        "code_turn_unverified": "三轮自动修复后仍未通过…",
    }
    with console.status("[cyan]正在启动 Coding Turn…[/]") as status:
        while not pending.done():
            try:
                events = await client.code_session_events(
                    session_id, after_sequence=sequence,
                )
                for event in events:
                    sequence = max(sequence, int(event.get("sequence", 0)))
                    label = labels.get(str(event.get("record_type", "")))
                    if label:
                        status.update(f"[cyan]{label}[/]")
            except Exception:
                # 主请求仍在 Gateway 中运行；短暂轮询失败不应取消 Coding Turn。
                pass
            await asyncio.sleep(0.5)
        return await pending


async def _handle_gateway_skill_command(
    client: GatewayClient,
    project_id: str,
    task: str,
) -> None:
    """通过 Gateway 管理 Skill，命令本身不进入 Session。"""
    try:
        parts = [_strip_cli_quote(value) for value in shlex.split(task, posix=False)]
        if len(parts) < 2:
            raise ValueError(_skill_usage())
        action = parts[1].lower()
        if action == "list":
            catalog = await client.skills(project_id)
            if not catalog:
                console.print("尚未安装可用 Skill。")
                return
            table = Table(title="已审核 Skill")
            table.add_column("名称", style="cyan")
            table.add_column("描述")
            table.add_column("位置")
            for item in catalog:
                table.add_row(str(item["name"]), str(item["description"]), str(item["location"]))
            console.print(table)
            return
        if action == "refresh":
            count = await client.refresh_skills(project_id)
            console.print(f"[green]Skill 目录与 Runtime Prompt 已刷新：{count} 个[/]")
            return
        if action == "audit":
            if len(parts) != 3:
                raise ValueError("用法：/skill audit <review-id>")
            report = await client.skill_audit(project_id, parts[2])
            console.print(
                f"状态：{report['status']}；文件：{report['total_files']}；"
                f"大小：{report['total_bytes']} 字节；报告：{report['report_path']}"
            )
            for finding in report.get("findings", []):
                console.print(
                    f"[yellow]{finding['severity']}[/] {finding['message']} "
                    f"{finding.get('path') or ''}"
                )
            return
        if action not in {"install", "update"}:
            raise ValueError(_skill_usage())
        position, name = 2, None
        if action == "update":
            if len(parts) <= position:
                raise ValueError("用法：/skill update <name> <source> [--ref REF] [--path PATH]")
            name, position = parts[position], position + 1
        if len(parts) <= position:
            raise ValueError(f"/skill {action} 缺少来源")
        options = _parse_skill_options(parts[position + 1 :])
        payload = {
            "project_id": project_id,
            "action": action,
            "source": parts[position],
            "name": name,
            "ref": options.get("ref"),
            "skill_path": options.get("skill_path"),
            "confirmed": action == "update" and typer.confirm("更新会替换现有 Skill，是否继续？", default=False),
        }
        if action == "update" and not payload["confirmed"]:
            console.print("[yellow]已取消 Skill 更新。[/]")
            return
        result = await client.manage_skill(payload)
        if result["status"] == "declined" and typer.confirm(
            f"{result['message']} 是否接受审核报告中的风险并重新安装？",
            default=False,
        ):
            payload["confirmed"] = True
            result = await client.manage_skill(payload)
        style = "green" if result["status"] == "installed" else "yellow"
        console.print(f"[{style}]{result['message']}[/]")
        if result.get("report_path"):
            console.print(f"[dim]审核报告：{result['report_path']}[/]")
    except Exception as exc:
        console.print(f"[red]{str(exc) or type(exc).__name__}[/]")


async def _chat(
    session_id: str | None,
    *,
    interrupt_controller: ChatInterruptController | None = None,
) -> None:
    """在一个 Runtime/Session 中处理多次用户输入，退出时触发 trace_end。"""
    console.print(
        "[bold cyan]Yuan Ye Agent[/]  输入 /help 查看命令，/exit 退出；"
        "运行中按 Ctrl+C 终止当前回答，空闲时按 Ctrl+C 退出。"
    )
    if session_id:
        console.print(f"[green]已恢复会话[/] {session_id}（{len(_memory().session_records(session_id))} 条消息）")
    config = load_runtime_config()
    runtime = AgentRuntime(
        config,
        approval=InteractiveApproval(console),
        retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=2),
        raise_errors=True,
    )
    interrupts = interrupt_controller or ChatInterruptController()
    interrupts.bind(asyncio.get_running_loop())
    try:
        while True:
            try:
                task = console.input("[bold blue]你 > [/]").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]已退出会话。[/]")
                return
            if task in {"/exit", "/quit"}:
                return
            if task == "/help":
                console.print(
                    "/compress 压缩当前上下文；/context refresh 刷新上下文；"
                    "/skill list|install|update|audit|refresh 管理 Skill；"
                    "/exit 退出；运行中 Ctrl+C 终止当前回答，空闲时 Ctrl+C 退出。"
                )
                continue
            if task == "/skill" or task.startswith("/skill "):
                await _handle_skill_command(runtime, task)
                continue
            if task:
                previous_id = session_id
                render_task = asyncio.create_task(
                    _render(runtime, task, session_id, propagate_errors=True)
                )
                interrupts.set_active(render_task)
                try:
                    session_id = await render_task
                except asyncio.CancelledError:
                    if not interrupts.consume_cancel_request():
                        raise
                    active_id = runtime.active_session_id or session_id or ""
                    session_id = active_id or session_id
                    console.print("[yellow]已终止当前回答，可继续输入下一条问题。[/]")
                    if active_id and not previous_id:
                        console.print(f"[dim]会话哈希：{active_id}；取消记录已保存[/]")
                    continue
                except Exception as exc:
                    active_id = runtime.active_session_id or session_id or ""
                    failure = runtime.last_failure or RuntimeFailure.capture(exc)
                    console.print(Panel(
                        f"[red]{str(exc) or type(exc).__name__}[/]",
                        title="Yuan Ye Agent 运行错误",
                    ))
                    await _handle_chat_failure(config, runtime, task, active_id, failure)
                    session_id = active_id or session_id
                    if active_id and not previous_id:
                        console.print(f"[dim]会话哈希：{active_id}；失败现场已保留，可继续本会话[/]")
                    continue
                finally:
                    interrupts.clear_active()
                if session_id and not previous_id:
                    console.print(f"[dim]会话哈希：{session_id}；下次可使用 chat --session {session_id} 恢复[/]")
    finally:
        await runtime.close()


async def _handle_skill_command(runtime: AgentRuntime, task: str) -> None:
    """处理不进入 Session JSONL 的显式 Skill 管理命令。"""
    if runtime.skills is None:
        console.print("[red]当前 Runtime 已禁用 Skill[/]")
        return
    try:
        parts = [_strip_cli_quote(value) for value in shlex.split(task, posix=False)]
        if len(parts) < 2:
            raise ValueError(_skill_usage())
        action = parts[1].lower()
        if action == "list":
            catalog = runtime.skills.catalog()
            if not catalog:
                console.print("尚未安装可用 Skill。")
                return
            table = Table(title="已审核 Skill")
            table.add_column("名称", style="cyan")
            table.add_column("描述")
            table.add_column("位置")
            for item in catalog:
                table.add_row(item.name, item.description, item.location)
            console.print(table)
            return
        if action == "refresh":
            if len(parts) != 2:
                raise ValueError("用法：/skill refresh")
            count = runtime.refresh_skills()
            console.print(f"[green]Skill 目录与 System Prompt 缓存已刷新：{count} 个可用 Skill[/]")
            return
        if action == "audit":
            if len(parts) != 3:
                raise ValueError("用法：/skill audit <review-id>")
            report = runtime.skills.audit_report(parts[2])
            table = Table(title=f"Skill 审核 {report.review_id}")
            table.add_column("等级")
            table.add_column("项目")
            table.add_column("路径")
            for finding in report.findings:
                table.add_row(finding.severity, finding.message, finding.path or "")
            console.print(
                f"状态：{report.status}；文件：{report.total_files}；"
                f"大小：{report.total_bytes} 字节；完整报告：{report.report_path}"
            )
            if report.findings:
                console.print(table)
            return
        if action not in {"install", "update"}:
            raise ValueError(_skill_usage())
        position = 2
        name = None
        if action == "update":
            if len(parts) <= position:
                raise ValueError("用法：/skill update <name> <source> [--ref REF] [--path PATH]")
            name = parts[position]
            position += 1
        if len(parts) <= position:
            raise ValueError(f"/skill {action} 缺少来源")
        source = parts[position]
        options = _parse_skill_options(parts[position + 1 :])
        request = SkillInstallRequest(
            source=source,
            action=action,
            name=name,
            ref=options.get("ref"),
            skill_path=options.get("skill_path"),
        )
        result = await runtime.skills.install(request)
        style = "green" if result.status == "installed" else "yellow"
        console.print(f"[{style}]{result.message}[/]")
        if result.candidates:
            console.print("候选 Skill：" + "、".join(result.candidates))
        if result.report_path:
            console.print(f"[dim]审核报告：{result.report_path}[/]")
    except Exception as exc:
        console.print(f"[red]{str(exc) or type(exc).__name__}[/]")


def _parse_skill_options(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    index = 0
    mapping = {"--ref": "ref", "--path": "skill_path", "--skill-path": "skill_path"}
    while index < len(values):
        key = values[index]
        target = mapping.get(key)
        if target is None or index + 1 >= len(values):
            raise ValueError(f"无效或缺少值的 Skill 选项：{key}")
        result[target] = values[index + 1]
        index += 2
    return result


def _strip_cli_quote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _skill_usage() -> str:
    return (
        "Skill 命令：/skill list；/skill refresh；/skill audit <review-id>；"
        "/skill install <source> [--ref REF] [--path PATH]；"
        "/skill update <name> <source> [--ref REF] [--path PATH]"
    )


async def _handle_chat_failure(config, runtime, task: str, session_id: str, failure: RuntimeFailure) -> None:
    """仅保存代码类缺陷现场，并由用户决定是否启动 Harness。"""
    if not failure.snapshot_worthy:
        # 网络、服务、配置、权限和普通运行错误已经由 CLI 展示。它们不能
        # 通过修改项目代码可靠解决，因此既不保存隐私敏感快照，也不加载
        # Harness 模块。网络重试最终成功时不会进入本函数。
        return

    harness = load_harness_module()
    writer = harness.ErrorSnapshotWriter(
        config.agent_root,
        secrets=(config.api_key or "",),
    )
    try:
        records = runtime.memory.session_records(session_id) if session_id and runtime.memory.has_session(session_id) else []
        snapshot = writer.capture(
            task=task,
            session_id=session_id,
            failure=failure,
            session_records=records,
            session_file=runtime.memory.active_filename(session_id) if session_id and records else "",
        )
    except Exception as snapshot_error:
        console.print(f"[yellow]错误现场保存失败：{str(snapshot_error) or type(snapshot_error).__name__}[/]")
        return
    console.print(f"[dim]错误复现快照：{snapshot}[/]")
    confirmed = typer.confirm("检测到可诊断的代码/模型格式缺陷，是否启动 Harness 隔离流水线？", default=False)
    writer.append_event(snapshot, "decision", repairable=True, confirmed=confirmed)
    if not confirmed:
        return
    request = harness.HarnessEvolutionRequest(
        project_root=config.agent_root,
        incident_id=snapshot.stem,
        snapshot_path=snapshot,
        task=task,
        config=config,
    )
    console.print("[cyan]Harness 正在检查主 worktree并准备隔离诊断…[/]")
    try:
        result = await harness.HarnessEvolutionRunner(writer).run(request)
    except Exception as evolution_error:
        writer.append_event(
            snapshot,
            "evolution",
            status="pipeline_error",
            message=str(evolution_error) or type(evolution_error).__name__,
        )
        console.print(f"[red]Harness 流水线失败：{str(evolution_error) or type(evolution_error).__name__}[/]")
        return
    style = "green" if result.merged else "yellow"
    console.print(f"[{style}]{result.message}[/]")


@session_app.command("list")
def session_list() -> None:
    """通过 Gateway 列出当前 workspace 的可恢复会话。"""
    async def load() -> list[dict[str, object]]:
        client = _gateway_client()
        project = await _gateway_project(client)
        return await client.sessions(str(project["project_id"]))

    sessions = asyncio.run(load())
    if not sessions:
        console.print("暂无可恢复会话。")
        return
    table = Table(title="本地会话")
    table.add_column("会话哈希", style="cyan")
    table.add_column("创建时间")
    table.add_column("消息数", justify="right")
    table.add_column("最新 JSONL")
    for item in sessions:
        table.add_row(str(item["session_id"]), str(item["created_at"]), str(item["message_count"]), str(item["latest_file"]))
    console.print(table)


@session_app.command("show")
def session_show(session_id: str) -> None:
    """通过 Gateway 显示指定会话最新分段。"""
    async def load() -> list[dict[str, object]]:
        client = _gateway_client()
        project = await _gateway_project(client)
        return await client.session(str(project["project_id"]), session_id)

    records = asyncio.run(load())
    table = Table(title=f"会话 {session_id}")
    table.add_column("时间", style="dim")
    table.add_column("角色", style="cyan")
    table.add_column("内容")
    for record in records:
        table.add_row(str(record.get("timestamp", "")), str(record.get("role", "")), str(record.get("content", "")))
    console.print(table)


@gateway_app.command("start")
def gateway_start(port: int | None = typer.Option(None, "--port")) -> None:
    """启动单实例本机 Gateway。"""
    config = load_runtime_config()
    manager = GatewayProcessManager(config.agent_root, port or config.gateway_port)
    status = manager.ensure_running()
    console.print(f"[green]Gateway 已运行[/] PID={status['pid']} {status['base_url']}")


@gateway_app.command("stop")
def gateway_stop(port: int | None = typer.Option(None, "--port")) -> None:
    """停止当前 Agent Home 的 Gateway。"""
    config = load_runtime_config()
    manager = GatewayProcessManager(config.agent_root, port or config.gateway_port)
    stopped = manager.stop()
    console.print("[green]Gateway 已停止[/]" if stopped else "[yellow]Gateway 当前未运行[/]")


@gateway_app.command("status")
def gateway_status(port: int | None = typer.Option(None, "--port")) -> None:
    """显示 Gateway 进程、地址和日志位置。"""
    config = load_runtime_config()
    status = GatewayProcessManager(config.agent_root, port or config.gateway_port).status()
    style = "green" if status["running"] else "yellow"
    console.print(
        f"[{style}]状态：{'running' if status['running'] else 'stopped'}[/]\n"
        f"PID：{status['pid'] or '-'}\n地址：{status['base_url']}\n日志：{status['log_path']}"
    )


@gateway_app.command("logs")
def gateway_logs(
    lines: int = typer.Option(100, "--lines", "-n", min=1, max=5000),
) -> None:
    """显示 Gateway 日志末尾内容。"""
    config = load_runtime_config()
    path = GatewayProcessManager(config.agent_root, config.gateway_port).log_path
    if not path.exists():
        console.print("尚无 Gateway 日志。")
        return
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    console.print("\n".join(content[-lines:]))


@gateway_app.command("run-internal", hidden=True)
def gateway_run_internal(
    port: int = typer.Option(8765, "--port"),
    agent_root: Path | None = typer.Option(None, "--agent-root"),
) -> None:
    """打包 sidecar 使用的前台服务入口。"""
    from gateway.process import run_gateway
    run_gateway(agent_root or default_agent_root(), port)


@app.command()
def serve_ui(port: int | None = typer.Option(None, "--port")) -> None:
    """自动启动 Gateway 并打开本机 Web 工作台。"""
    serve(port)


def main() -> None:
    """供源码入口和打包命令调用。"""
    if not sys.argv[1:] or sys.argv[1] != "init":
        result = ensure_project_initialized(default_agent_root())
        if result.initialized:
            console.print(f"[green]首次运行初始化完成[/] {result.yy_dir}")
            console.print(f"请按需编辑 {result.yy_dir / 'settings.local.json'}；后续启动不会重复初始化。")
    app()
