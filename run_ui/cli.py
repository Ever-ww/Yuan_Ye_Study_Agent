"""Textual conversation client with a Rich compatibility command surface."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import shlex
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
import json

from Agent import (
    AgentRuntime,
    EventType,
    ModelRetryPolicy,
    RuntimeFailure,
    default_agent_root,
    load_runtime_config,
    prepare_default_agent_root,
)
from bootstrap import ensure_project_initialized, initialize_project
from memory import MemoryStore
from gateway import GatewayClient, GatewayProcessManager
from gateway.models import GatewayEventEnvelope
from cron import (
    CronJobCreateRequest,
    CronJobEditRequest,
    CronPaperResearchPresetRequest,
    CronSchedule,
    CronScheduleCalculator,
)
from skill import SkillInstallRequest
from .approval import InteractiveApproval, active_live as _active_live
from .chat_tui import YuanYeChatApp
from .web import serve
from backup import BackupService, EncryptedBackupArchive, RestoreService, read_manifest

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Yuan Ye Study Agent 本地入口")
session_app = typer.Typer(help="列出、查看和恢复本地会话")
gateway_app = typer.Typer(help="管理本机 Gateway 后台进程")
app.add_typer(session_app, name="session")
app.add_typer(gateway_app, name="gateway")
cron_app = typer.Typer(help="管理 Gateway 后台 Cron 与 Heartbeat")
app.add_typer(cron_app, name="cron")
backup_app = typer.Typer(help="创建、验证和恢复去重的 Agent Home 快照")
app.add_typer(backup_app, name="backup")
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
    yy = initialize_project(prepare_default_agent_root())
    console.print(f"[green]初始化完成[/] {yy}")
    console.print(f"请编辑 {yy / 'settings.local.json'} 配置模型；已有文件不会被覆盖。")


    _offer_initial_paper_research_cron(yy)


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


async def _create_initial_paper_research_cron(
    expression: str,
    timezone_name: str,
):
    client = _gateway_client()
    project = await _gateway_project(client)
    return await client.initialize_paper_research_cron(CronPaperResearchPresetRequest(
        project_id=str(project["project_id"]),
        expression=expression,
        timezone=timezone_name,
    ))


def _offer_initial_paper_research_cron(yy_dir: Path) -> None:
    """Offer the built-in schedule only during an interactive first-run flow."""
    if not sys.stdin.isatty():
        return
    if not typer.confirm("是否开启每周固定时间的自动论文调研？", default=False):
        return
    expression = typer.prompt("五段 Cron 表达式", default="0 9 * * 1")
    timezone_name = typer.prompt(
        "时区",
        default=CronScheduleCalculator.local_timezone(),
    )
    try:
        job = asyncio.run(_create_initial_paper_research_cron(expression, timezone_name))
    except Exception as exc:
        console.print(f"[red]论文调研 Cron 初始化失败：[/] {exc}")
        console.print("可修正配置后使用 `yy cron add` 或让 Agent 调用 cronjob 工具重新创建。")
        return
    console.print(
        f"[green]已开启论文调研 Cron[/] {job.job_id}\n"
        f"计划：{job.schedule.expression}（{job.schedule.timezone}）\n"
        f"持久化：{yy_dir / 'cron' / 'jobs.json'}"
    )


class _GatewayLiveView:
    """One stable Rich surface for Main output and the per-Turn Observer."""

    def __init__(
        self,
        main_content: str | Text,
        observer_content: str,
        *,
        phase: str,
        height: int | None = None,
        history_content: str | None = None,
    ) -> None:
        # Kept as the user-visible plain projection for lightweight terminal
        # adapters and read-receipt tests. Rich itself renders ``_panel``.
        self.renderable = main_content.plain if isinstance(main_content, Text) else main_content
        self.history_content = history_content
        self.observer_content = observer_content
        self.phase = phase
        grid = Table(
            expand=True,
            # The Panel supplies the outer border. MINIMAL preserves the header
            # rule and adds one continuous divider between Main and Observer.
            box=box.MINIMAL,
            show_edge=False,
            pad_edge=False,
            padding=(0, 1),
        )
        grid.add_column(
            "MAIN AGENT", ratio=2, min_width=24,
            overflow="fold", header_style="bold cyan",
        )
        grid.add_column(
            "TURN OBSERVER", ratio=1, min_width=22,
            overflow="fold", header_style="bold magenta",
        )
        grid.add_row(
            (
                main_content
                if isinstance(main_content, Text)
                else Text.from_markup(main_content or "正在运行…")
            ),
            Markdown(observer_content or "正在建立本轮监控…"),
        )
        border = "green" if phase == "已完成" else "red" if phase == "已结束" else "cyan"
        self._panel = Panel(
            grid, title=f"Yuan Ye · {phase}", border_style=border, height=height,
        )

    def __rich_console__(self, console, options):
        del console, options
        yield self._panel


def _live_main_window(main_content: str, selected_console: Console) -> Text:
    """Return a bounded, width-aware tail for the mutable Live surface."""

    full = Text.from_markup(main_content or "正在运行…")
    # Account for the outer Panel, two cell paddings and the column separator.
    main_width = max(20, ((selected_console.size.width - 8) * 2 // 3) - 2)
    max_lines = max(3, selected_console.size.height - 8)
    wrapped = full.wrap(selected_console, main_width, overflow="fold")
    if len(wrapped) <= max_lines:
        return full
    clipped = Text("… 较早内容已滚动；任务结束后显示完整结果 …", style="dim")
    for line in wrapped[-(max_lines - 1):]:
        clipped.append("\n")
        clipped.append_text(line)
    return clipped


def _gateway_live_height(selected_console: Console) -> int:
    """Reserve one terminal row while keeping the answer surface stable."""

    return max(7, selected_console.size.height - 1)


async def _wait_for_observer_terminal(
    client: GatewayClient,
    run_id: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any] | None:
    """Briefly await the isolated terminal projection without delaying output."""
    getter = getattr(client, "observer_status", None)
    if not callable(getter):
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last: dict[str, Any] | None = None
    while True:
        try:
            value = await getter(run_id)
            if isinstance(value, dict):
                last = value
                if value.get("status") in {"finalized", "failed"}:
                    return value
        except Exception as exc:
            # The Observer instance may not exist yet because its first
            # milestone is deliberately processed off the Main output path.
            # Retry only inside this small terminal grace window.
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if not isinstance(exc, (KeyError, LookupError)) and status_code not in {404, 409, 500}:
                return last
        remaining = deadline - loop.time()
        if remaining <= 0:
            return last
        await asyncio.sleep(min(0.1, remaining))


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
    observer_progress = "正在建立本轮监控…"
    handled_observer_proposals: set[str] = set()
    completed_view: _GatewayLiveView | None = None
    approval_selector = InteractiveApproval(console)
    initial_view = _GatewayLiveView(
        "正在排队…", observer_progress, phase="准备中",
        height=_gateway_live_height(console), history_content="正在排队…",
    )
    async with contextlib.AsyncExitStack() as stack:
        # Persistent chat owns the sole Textual UI. One-shot ``run`` and
        # ``chat --classic`` stay on the lightweight Rich projection.
        live = stack.enter_context(Live(
            initial_view,
            console=console,
            refresh_per_second=12,
            vertical_overflow="crop",
            transient=True,
        ))
        token = _active_live.set(live)

        async def decide_observer_proposal(proposal: dict[str, object]) -> None:
            proposal_id = str(proposal.get("proposal_id") or "")
            if not proposal_id or proposal_id in handled_observer_proposals:
                return
            handled_observer_proposals.add(proposal_id)
            live.stop()
            try:
                action = typer.prompt(
                    "Observer 检测到意图偏移（adopt/edit/reject）",
                    default="reject",
                ).strip().lower()
                if action not in {"adopt", "edit", "reject"}:
                    action = "reject"
                edited_prompt = None
                if action == "edit":
                    edited_prompt = typer.prompt(
                        "编辑纠偏提示词",
                        default=str(proposal.get("proposed_prompt") or ""),
                    )
                await client.decide_observer_correction(
                    proposal_id,
                    expected_revision=int(proposal.get("revision", 0)),
                    action=action,
                    edited_prompt=edited_prompt,
                    reason=f"cli_user_{action}",
                )
                lines.append(
                    f"[yellow]Observer 纠偏建议：{proposal.get('proposed_prompt') or ''}[/]"
                )
            finally:
                live.start(refresh=True)

        try:
            # 保证正常结束、Ctrl+C 与异常退出都在 asyncio.run 关闭事件循环前
            # 完成订阅器及其底层 WebSocket 的单次、有序 aclose。
            async with contextlib.aclosing(client.subscribe(run.run_id)) as subscription:
                async for event in subscription:
                    if event.session_id:
                        active_session_id = event.session_id
                    if event.type == EventType.TEXT.value:
                        # Model text is data, not Rich markup. Escaping each
                        # chunk prevents Markdown links or bracketed text from
                        # being consumed as terminal style tags.
                        streaming_text += escape(str(event.payload.get("content", "")))
                    elif event.type == EventType.MODEL_RETRY.value:
                        if streaming_text:
                            lines.append("[yellow]网络中断前的不完整流式片段已丢弃[/]")
                            streaming_text = ""
                        lines.append(
                            f"[yellow]模型网络异常，{event.payload.get('delay_seconds', 2)} 秒后重试[/]"
                        )
                    elif event.type == EventType.MODEL_RECONNECTED.value:
                        lines.append("[green]模型网络连接已恢复[/]")
                    elif event.type == EventType.SANDBOX_FALLBACK.value:
                        lines.append(
                            f"[yellow]{event.payload.get('message', '沙箱后端不可用，已进入 checkpoint-only；Bash/Shell 已禁用')}[/]"
                        )
                    elif event.type == EventType.TOOL_REQUESTED.value:
                        if streaming_text:
                            lines.append(streaming_text)
                            streaming_text = ""
                        lines.append(
                            f"[cyan]工具请求[/] {escape(str(event.payload.get('name', '')))}"
                        )
                    elif event.type == EventType.TOOL_COMPLETED.value:
                        tool_name = escape(str(event.payload.get("name", "")))
                        if event.payload.get("status") == "error":
                            lines.append(f"[red]工具失败[/] {tool_name}")
                        else:
                            lines.append(f"[green]工具完成[/] {tool_name}")
                        reference = event.payload.get("observation_id")
                        if isinstance(reference, str) and reference:
                            lines.append(f"[dim]详情：/tool-result {escape(shlex.quote(reference))}[/]")
                    elif event.type == "observer_progress":
                        observer_progress = str(
                            event.payload.get("progress_markdown") or observer_progress
                        )
                    elif event.type == "observer_correction_proposed":
                        await decide_observer_proposal(dict(event.payload))
                    elif event.type == EventType.GATEWAY_RESTART_REQUIRED.value:
                        lines.append(f"[bold yellow]{event.payload.get('message', '需要重启 Gateway')}[/]")
                    elif event.type == "approval_requested":
                        approved = await approval_selector(
                            str(event.payload.get("tool_name", "")),
                            dict(event.payload.get("arguments", {})),
                        )
                        # Gateway 的持久化 expires_at 是超时权威来源；本地菜单超时后
                        # 不再发送一个可能与 TIMEOUT 状态竞争的重复拒绝决定。
                        if not approval_selector.last_timed_out:
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
                        lines.append(f"[red]{escape(terminal_error)}[/]")
                    elif event.type == "harness_evolution_proposed":
                        live.stop()
                        confirmed = typer.confirm(
                            "检测到可修复的 Yuan Ye 源码缺陷。是否启动隔离 Harness 自动修复（最多 4 次）？",
                            default=False,
                        )
                        try:
                            result = await client.decide_harness_evolution(
                                str(event.payload["proposal_id"]), confirmed,
                            )
                            status = str((result.get("result") or {}).get("status") or result.get("status"))
                            lines.append(
                                f"[{'green' if status == 'merged' else 'yellow'}]"
                                f"Harness Proposal：{status}[/]"
                            )
                        finally:
                            live.start(refresh=True)
                    elif event.type == "run_completed":
                        answer = str(event.payload.get("answer", ""))
                        if answer:
                            # The terminal event carries the authoritative full
                            # answer. Replace any incomplete/coalesced stream
                            # projection so completion can never show a blank or
                            # fragmented left pane.
                            streaming_text = escape(answer)
                    # Keep the entire Turn in the presentation buffer. The
                    # viewport, rather than destructive list slicing, decides
                    # which fixed-height window is currently visible.
                    display = lines + ([streaming_text] if streaming_text else [])
                    terminal = event.type in {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}
                    main_content = "\n".join(display) or "正在运行…"
                    phase = (
                        "已完成" if event.type == "run_completed"
                        else "已结束" if terminal else "运行中"
                    )
                    if terminal:
                        observer_progress = (
                            "✓ 主任务已完成\n\n正在完成最终意图核对…"
                            if event.type == "run_completed"
                            else "主任务已结束\n\n正在完成最终状态核对…"
                        )
                    live.update(
                        _GatewayLiveView(
                            _live_main_window(main_content, console),
                            observer_progress,
                            phase=phase,
                            height=_gateway_live_height(console),
                            history_content=main_content,
                        ),
                        refresh=terminal,
                    )
                    if terminal:
                        # A correction proposal is created from the terminal
                        # Observer state, so it can be sequenced after the Run's
                        # terminal event. Querying the durable projection avoids
                        # losing it when a client closes its live subscription.
                        try:
                            status = await _wait_for_observer_terminal(client, run.run_id)
                            if status is None:
                                status = {}
                            if status.get("status") in {"finalized", "failed"}:
                                observer_progress = str(
                                    status.get("progress_markdown") or observer_progress
                                )
                            proposal = status.get("correction_proposal")
                            if isinstance(proposal, dict):
                                if proposal.get("status") == "pending":
                                    await decide_observer_proposal(proposal)
                        except Exception:
                            # Observer status is an isolated enhancement. It must
                            # not prevent display/acknowledgement of the Run result.
                            pass
                        if "正在完成最终" in observer_progress:
                            observer_progress = (
                                "✓ 任务已完成"
                                if event.type == "run_completed"
                                else "⚠ 主任务已结束"
                            )
                        live.update(
                            _GatewayLiveView(
                                _live_main_window(main_content, console),
                                observer_progress,
                                phase=phase,
                                height=_gateway_live_height(console),
                                history_content=main_content,
                            ),
                            refresh=True,
                        )
                        completed_view = _GatewayLiveView(
                            main_content, observer_progress, phase=phase,
                            history_content=main_content,
                        )
                        try:
                            await client.acknowledge_run_result(event.run_id)
                        except Exception:
                            # Read receipts must not turn a displayed, successful Run
                            # into a failure or cause its model/tools to be rerun.
                            console.print("[yellow]结果已显示，但 Inbox 已读确认失败；可稍后用 /inbox 查看。[/]")
        except asyncio.CancelledError:
            await client.cancel_run(run.run_id)
            raise
        finally:
            _active_live.reset(token)
    if completed_view is not None:
        # The final Rich projection enters ordinary terminal scrollback and
        # does not need a second fixed-height Textual lifecycle.
        console.print(completed_view, crop=False)
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
                    elif event.type is EventType.SANDBOX_FALLBACK:
                        lines.append(
                            f"[yellow]{event.payload.get('message', '沙箱后端不可用，已进入 checkpoint-only；Bash/Shell 已禁用')}[/]"
                        )
                    elif event.type is EventType.TOOL_REQUESTED:
                        if streaming_text:
                            lines.append(streaming_text)
                            streaming_text = ""
                        lines.append(f"[cyan]工具请求[/] {event.payload['name']}")
                    elif event.type is EventType.TOOL_COMPLETED:
                        if event.payload.get("status") == "error":
                            lines.append(f"[red]工具失败[/] {event.payload['name']}")
                        else:
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
def chat(
    session_id: str | None = typer.Option(None, "--session", "-s", help="恢复指定会话哈希"),
    continue_last: bool = typer.Option(
        False, "--continue", help="恢复当前 workspace 最近使用的 Session",
    ),
    classic: bool = typer.Option(
        False, "--classic", help="使用兼容性的逐行终端界面",
    ),
) -> None:
    """连接 Gateway 并启动连续交互会话。"""
    if session_id and continue_last:
        raise typer.BadParameter("--session 与 --continue 不能同时使用")
    use_tui = console.is_terminal and not classic
    interrupts = ChatInterruptController()
    previous_handler = signal.getsignal(signal.SIGINT)
    if not use_tui:
        signal.signal(signal.SIGINT, interrupts.handle_sigint)
    try:
        asyncio.run(_chat_gateway(
            session_id,
            continue_last=continue_last,
            interrupt_controller=interrupts,
            use_tui=use_tui,
        ))
    except KeyboardInterrupt:
        console.print("\n[dim]已退出会话。[/]")
    except Exception as exc:
        console.print(Panel(f"[red]{str(exc) or type(exc).__name__}[/]", title="Yuan Ye Agent 配置错误"))
        raise typer.Exit(code=1) from exc
    finally:
        if not use_tui:
            signal.signal(signal.SIGINT, previous_handler)


async def _latest_session_observer_status(
    client: GatewayClient,
    project_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Restore the newest durable Observer projection for a chat Session."""
    try:
        runs = await client.runs(project_id)
    except Exception:
        # Observer is a presentation enhancement. A missing/older Gateway API
        # must not prevent the conversation itself from being restored.
        return None
    for run in runs:
        if run.session_id != session_id or run.workload_kind != "chat":
            continue
        try:
            return await client.observer_status(run.run_id)
        except Exception:
            # A Run may predate Observer support or have failed before the
            # first visible event. Continue to the previous Turn in Session.
            continue
    return None


async def _chat_gateway(
    session_id: str | None,
    *,
    continue_last: bool = False,
    interrupt_controller: ChatInterruptController,
    use_tui: bool = False,
) -> None:
    if not use_tui:
        console.print(
            "[bold cyan]Yuan Ye Gateway[/]  输入 /help 查看命令，/exit 退出；"
            "运行中按 Ctrl+C 终止当前回答。"
        )
    client = _gateway_client()
    project = await _gateway_project(client)
    project_id = str(project["project_id"])
    initial_notices: list[str] = []
    if continue_last:
        sessions = await client.sessions(project_id)
        if not sessions:
            message = "当前 workspace 还没有可恢复的 Session，将创建新会话。"
            if use_tui:
                initial_notices.append(message)
            else:
                console.print(f"[dim]{message}[/]")
        else:
            session_id = str(sessions[0]["session_id"])
    records: list[dict[str, Any]] = []
    if session_id:
        sessions = await client.sessions(project_id)
        if not any(item.get("session_id") == session_id for item in sessions):
            raise ValueError(f"当前 workspace 未找到 Session：{session_id}")
        records = list(await client.session(project_id, session_id))
        if not use_tui:
            console.print(f"[green]已恢复会话[/] {session_id}（{len(records)} 条记录）")
            _render_restored_history(records)
            warning = await _acknowledge_displayed_history(
                client, project_id, session_id, records,
            )
            if warning:
                console.print(f"[yellow]{warning}[/]")
    if use_tui:
        model_options = await client.model_options(session_id, project_id)
        selected_model = next(
            (item.model for item in model_options if item.selected),
            model_options[0].model if model_options else None,
        )
        unread_count = await _prepare_actionable_inbox(client)
        initial_observer_status = (
            await _latest_session_observer_status(client, project_id, session_id)
            if session_id else None
        )
        tui: YuanYeChatApp | None = None

        async def external_command(
            command: str, active_session_id: str | None,
        ) -> str | None:
            original_width = console._width
            if tui is not None:
                console.width = tui.command_output_width()
            try:
                with console.capture() as captured:
                    if command == "/skill" or command.startswith("/skill "):
                        await _handle_gateway_skill_command(
                            client, project_id, active_session_id, command,
                            confirm=tui.confirm if tui is not None else None,
                        )
                    elif command == "/inbox" or command.startswith("/inbox "):
                        await _handle_inbox_command(client, command)
                    elif command == "/tool-result" or command.startswith("/tool-result "):
                        await _handle_tool_result_command(
                            client, project_id, active_session_id, command,
                        )
                    elif command == "/cron" or command.startswith("/cron "):
                        await _handle_cron_command(client, project_id, command)
                    elif command == "/dream" or command.startswith("/dream "):
                        await _handle_dream_command(client, command)
                    elif command == "/harness" or command.startswith("/harness "):
                        await _handle_harness_command(
                            client, command,
                            confirm=tui.confirm if tui is not None else None,
                        )
                    elif command == "/extension" or command.startswith("/extension "):
                        await _handle_extension_command(client, command)
                    elif command == "/reload" or command.startswith("/reload "):
                        await _handle_runtime_reload_command(client, command)
                    else:
                        # /compress and /context refresh are Runtime commands and
                        # intentionally travel through the ordinary Run API.
                        raise ValueError(f"未知本地命令：{command}")
            finally:
                console._width = original_width
            return Text.from_ansi(captured.get()).plain.strip() or None

        async def history_displayed() -> str | None:
            if session_id and records:
                return await _acknowledge_displayed_history(
                    client, project_id, session_id, records,
                )
            return None

        tui = YuanYeChatApp(
            client,
            project_id,
            session_id=session_id,
            records=records,
            external_command=external_command,
            history_displayed=history_displayed,
            unread_count=unread_count,
            initial_observer_status=initial_observer_status,
            model_name=selected_model,
            model_options=model_options,
            initial_notices=tuple(initial_notices),
        )
        await tui.run_async(mouse=True)
        return
    interrupt_controller.bind(asyncio.get_running_loop())
    await _notify_actionable_inbox(client)
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
                "/inbox [all|show <ID>|read <ID>|read-all]；"
                "/tool-result <record_id|tool_call_id> [字符偏移]；"
                "/extension status|grant|revoke|reenable（管理员 override）；"
                "/reload [status|approve <plan_hash>|rollback <plugin> <generation>]；"
                "/cron list|status|add|at|preview|edit|pause|resume|run|remove；"
                "/dream status|run|backfill|rollback；"
                "运行中 Ctrl+C 取消当前 Run，空闲时 Ctrl+C 退出客户端。"
            )
            continue
        if task == "/code":
            await _code_mode(client, project_id, session_id)
            continue
        if task == "/skill" or task.startswith("/skill "):
            await _handle_gateway_skill_command(client, project_id, session_id, task)
            continue
        if task == "/inbox" or task.startswith("/inbox "):
            await _handle_inbox_command(client, task)
            continue
        if task == "/tool-result" or task.startswith("/tool-result "):
            await _handle_tool_result_command(client, project_id, session_id, task)
            continue
        if task == "/cron" or task.startswith("/cron "):
            await _handle_cron_command(client, project_id, task)
            continue
        if task == "/dream" or task.startswith("/dream "):
            await _handle_dream_command(client, task)
            continue
        if task == "/harness" or task.startswith("/harness "):
            await _handle_harness_command(client, task)
            continue
        if task == "/extension" or task.startswith("/extension "):
            await _handle_extension_command(client, task)
            continue
        if task == "/reload" or task.startswith("/reload "):
            await _handle_runtime_reload_command(client, task)
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


async def _handle_extension_command(client: GatewayClient, task: str) -> None:
    """Administrative Extension overrides; normal grants happen during /code."""
    from gateway.models import ExtensionGrantRequest, ExtensionReenableRequest

    parts = task.split(maxsplit=2)
    action = parts[1].lower() if len(parts) > 1 else "status"
    if action == "status":
        selected = await client.extension_status(parts[2] if len(parts) > 2 else None)
        console.print_json(data=selected)
        return
    if action not in {"grant", "revoke", "reenable"} or len(parts) < 3:
        raise ValueError(
            "Usage: /extension status [hook_id] or /extension grant|revoke|reenable <JSON>"
        )
    payload = json.loads(parts[2])
    if action == "reenable":
        result = await client.extension_reenable(ExtensionReenableRequest.model_validate(payload))
    else:
        result = await client.extension_grant(
            ExtensionGrantRequest.model_validate(payload), revoke=action == "revoke",
        )
    console.print_json(data=result)


async def _handle_runtime_reload_command(client: GatewayClient, task: str) -> None:
    """Build or inspect immutable Runtime resource generations."""
    parts = shlex.split(task)
    action = parts[1].lower() if len(parts) > 1 else "reload"
    if action == "status":
        console.print_json(data=await client.runtime_plugin_status())
        return
    if action == "approve":
        if len(parts) != 3:
            raise ValueError("Usage: /reload approve <plan_hash>")
        result = await client.reload_runtime_plugins(
            actor=client.client_id, approved_plan_hash=parts[2],
        )
    elif action == "rollback":
        if len(parts) != 4:
            raise ValueError("Usage: /reload rollback <plugin_id> <generation_id>")
        result = await client.rollback_runtime_plugin(
            parts[2], parts[3], actor=client.client_id,
        )
    elif action == "reload" and len(parts) in {1, 2}:
        result = await client.reload_runtime_plugins(actor=client.client_id)
    else:
        raise ValueError(
            "Usage: /reload [status|approve <plan_hash>|rollback <plugin_id> <generation_id>]"
        )
    console.print_json(data=result)


async def _handle_cron_command(client: GatewayClient, project_id: str, task: str) -> None:
    """处理聊天内的轻量 `/cron` 管理命令，不写入普通 Session。"""
    try:
        parts = shlex.split(task)
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action == "list":
            _render_cron_jobs(await client.cron_jobs(project_id))
        elif action == "status":
            console.print((await client.cron_status()).model_dump_json(indent=2))
        elif action == "preview" and len(parts) in {3, 4}:
            timezone_name = parts[3] if len(parts) == 4 else CronScheduleCalculator.local_timezone()
            result = await client.cron_preview(CronSchedule(
                kind="cron", expression=parts[2], timezone=timezone_name,
            ))
            console.print("\n".join(_cron_preview_local(result)) or "没有后续执行时间")
        elif action == "add" and len(parts) >= 3 and parts[2].startswith("--"):
            options = _cron_options(parts[2:])
            every, expression = options.get("every"), options.get("cron")
            if bool(every) == bool(expression):
                raise ValueError("必须且只能提供 --every 或 --cron")
            schedule = (
                CronSchedule(kind="interval", interval_seconds=_parse_interval(str(every)))
                if every else CronSchedule(
                    kind="cron", expression=expression,
                    timezone=options.get("timezone") or CronScheduleCalculator.local_timezone(),
                )
            )
            result = await client.create_cron(CronJobCreateRequest(
                project_id=project_id,
                name=_required_option(options, "name"),
                prompt=_required_option(options, "prompt"),
                schedule=schedule,
            ))
            console.print(f"[green]已创建[/] {result.job_id}")
        elif action == "add" and len(parts) >= 6 and parts[2] == "every":
            result = await client.create_cron(CronJobCreateRequest(
                project_id=project_id,
                name=parts[4],
                prompt=" ".join(parts[5:]),
                schedule=CronSchedule(
                    kind="interval", interval_seconds=_parse_interval(parts[3]),
                ),
            ))
            console.print(f"[green]已创建[/] {result.job_id}")
        elif action == "add" and len(parts) >= 8 and parts[2] == "cron":
            result = await client.create_cron(CronJobCreateRequest(
                project_id=project_id,
                name=parts[6],
                prompt=" ".join(parts[7:]),
                schedule=CronSchedule(
                    kind="cron", expression=parts[3], timezone=parts[5],
                ),
            ))
            console.print(f"[green]已创建[/] {result.job_id}")
        elif action == "at" and len(parts) >= 5 and not any(item.startswith("--") for item in parts[3:]):
            result = await client.create_cron(CronJobCreateRequest(
                project_id=project_id,
                name=parts[3],
                prompt=" ".join(parts[4:]),
                schedule=CronSchedule(kind="once", run_at=parts[2]),
            ))
            console.print(f"[green]已创建[/] {result.job_id}")
        elif action == "at" and len(parts) >= 3:
            options = _cron_options(parts[3:])
            result = await client.create_cron(CronJobCreateRequest(
                project_id=project_id,
                name=_required_option(options, "name"),
                prompt=_required_option(options, "prompt"),
                schedule=CronSchedule(kind="once", run_at=parts[2]),
            ))
            console.print(f"[green]已创建[/] {result.job_id}")
        elif action == "edit" and len(parts) >= 3:
            options = _cron_options(parts[3:])
            every, expression = options.get("every"), options.get("cron")
            if every and expression:
                raise ValueError("--every 与 --cron 不能同时使用")
            schedule = None
            if every:
                schedule = CronSchedule(kind="interval", interval_seconds=_parse_interval(every))
            elif expression:
                schedule = CronSchedule(
                    kind="cron", expression=expression,
                    timezone=options.get("timezone") or CronScheduleCalculator.local_timezone(),
                )
            if options.get("name") is None and options.get("prompt") is None and schedule is None:
                raise ValueError("edit 至少需要 --name、--prompt、--every 或 --cron")
            result = await client.edit_cron(parts[2], CronJobEditRequest(
                name=options.get("name"), prompt=options.get("prompt"), schedule=schedule,
            ))
            console.print(f"[green]已更新[/] {result.job_id}")
        elif action in {"pause", "resume", "run", "remove"} and len(parts) == 3:
            operation = getattr(client, f"{action}_cron")
            result = await operation(parts[2])
            console.print(f"[green]{action} 完成[/] {result.job_id}")
        else:
            raise ValueError(
                "用法：/cron list|status；/cron preview <表达式> [时区]；"
                "/cron add every <30m> <名称> <Prompt>；"
                "/cron add cron <表达式> --timezone <时区> <名称> <Prompt>；"
                "/cron at <ISO时间> --name <名称> --prompt <Prompt>；"
                "/cron edit <ID> [--name/--prompt/--every/--cron]；"
                "/cron pause|resume|run|remove <ID>"
            )
    except Exception as exc:
        console.print(f"[red]Cron 命令失败：{str(exc) or type(exc).__name__}[/]")


def _cron_options(parts: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    index = 0
    while index < len(parts):
        key = parts[index]
        if not key.startswith("--") or index + 1 >= len(parts):
            raise ValueError(f"无效 Cron 选项：{key}")
        values[key[2:].replace("-", "_")] = parts[index + 1]
        index += 2
    return values


def _required_option(options: dict[str, str], name: str) -> str:
    value = options.get(name, "").strip()
    if not value:
        raise ValueError(f"缺少 --{name.replace('_', '-')}")
    return value


async def _handle_inbox_command(client: GatewayClient, task: str) -> None:
    """在交互式 CLI 中查询和管理 Gateway 后台结果。"""
    try:
        parts = shlex.split(task)
    except ValueError as exc:
        console.print(f"[red]Inbox 命令解析失败：{exc}[/]")
        return
    action = parts[1].lower() if len(parts) > 1 else "unread"
    if action in {"unread", "all"} and len(parts) == 1 + (action != "unread"):
        items = await client.inbox(unread_only=action != "all")
        _render_inbox_table(items, unread_only=action != "all")
        return
    if action == "show" and len(parts) == 3:
        item = await _resolve_inbox_item(client, parts[2])
        if item is not None:
            _render_inbox_item(item)
            if not bool(item.get("read")):
                await client.mark_inbox_read(str(item["item_id"]))
        return
    if action == "read" and len(parts) == 3:
        item = await _resolve_inbox_item(client, parts[2])
        if item is None:
            return
        updated = await client.mark_inbox_read(str(item["item_id"]))
        console.print(f"[green]已标记为已读：[/]{updated['item_id']}")
        return
    if action == "read-all" and len(parts) == 2:
        items = await client.inbox(unread_only=True)
        for item in items:
            await client.mark_inbox_read(str(item["item_id"]))
        console.print(f"[green]已将 {len(items)} 条 Inbox 结果标记为已读。[/]")
        return
    console.print(
        "[yellow]用法：/inbox；/inbox all；/inbox show <ID>；"
        "/inbox read <ID>；/inbox read-all[/]"
    )


async def _notify_actionable_inbox(client: GatewayClient) -> None:
    """Silence routine maintenance and report only an actionable count.

    Successful/no-op background results remain in ``/inbox all`` as audit
    history but are silently acknowledged.  Failures remain unread, while chat
    startup shows only one compact hint instead of expanding maintenance rows.
    """
    count = await _prepare_actionable_inbox(client)
    if count:
        console.print(
            f"[yellow]后台有 {count} 条需要处理的通知；"
            "使用 /inbox 查看详情。[/]"
        )


async def _prepare_actionable_inbox(client: GatewayClient) -> int:
    """Acknowledge routine results and return the actionable unread count."""
    try:
        unread = await client.inbox(unread_only=True)
    except Exception:
        # Inbox is a projection/notification surface. An outage must not make
        # the primary chat client unusable.
        return 0
    if not unread:
        return 0
    actionable: list[dict[str, object]] = []
    for item in unread:
        status = str(item.get("status", "")).casefold()
        if status not in {"completed", "succeeded", "success"}:
            actionable.append(item)
            continue
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            continue
        try:
            await client.mark_inbox_read(item_id)
        except Exception:
            # A receipt outage must not prevent an interactive chat from
            # starting. The completed row may be acknowledged next time.
            continue
    return len(actionable)


async def _resolve_inbox_item(
    client: GatewayClient,
    item_id_or_prefix: str,
) -> dict[str, object] | None:
    """允许使用表格中显示的唯一 ID 前缀定位一条结果。"""
    items = await client.inbox(unread_only=False)
    exact = [item for item in items if str(item.get("item_id", "")) == item_id_or_prefix]
    matches = exact or [
        item for item in items
        if str(item.get("item_id", "")).startswith(item_id_or_prefix)
    ]
    if not matches:
        console.print(f"[red]未找到 Inbox 项：{item_id_or_prefix}[/]")
        return None
    if len(matches) > 1:
        console.print(f"[red]Inbox ID 前缀不唯一，请输入更多字符：{item_id_or_prefix}[/]")
        return None
    return dict(matches[0])


async def _handle_tool_result_command(client, project_id: str, session_id: str | None, task: str) -> None:
    """On-demand canonical observation detail; never execute the original tool."""
    if not session_id:
        console.print("[yellow]请先开始或恢复一个 Session。[/]")
        return
    try:
        parts = shlex.split(task)
        if len(parts) not in {2, 3}:
            raise ValueError("用法：/tool-result <record_id|tool_call_id> [字符偏移]")
        offset = int(parts[2]) if len(parts) == 3 else 0
        if offset < 0:
            raise ValueError("字符偏移不能为负数")
        result = await client.session_tool_result(project_id, session_id, record_id=parts[1], content_offset=offset)
        if not result.get("records"):
            result = await client.session_tool_result(project_id, session_id, tool_call_id=parts[1], content_offset=offset)
        if result.get("ambiguous"):
            console.print("调用 ID 对应多条历史记录，请使用以下精确 record_id：")
            for record in result.get("matches", []):
                console.print(str(record.get("record_id")), markup=False)
            return
        if not result.get("records"):
            console.print("当前 Session 中未找到该工具结果。")
            return
        for record in result["records"]:
            from rich.text import Text
            console.print(Panel(Text(str(record.get("content", ""))), title="历史工具结果（非重新执行）"))
            console.print(f"工具={record.get('name')} 状态={record.get('status')} record_id={record.get('record_id')}", markup=False)
            console.print(f"content_sha256={record.get('content_sha256')}", markup=False)
            if record.get("next_content_offset") is not None:
                reference = record.get("record_id") or record.get("tool_call_id")
                console.print(f"下一页：/tool-result {shlex.quote(str(reference))} {record['next_content_offset']}", markup=False)
    except Exception as exc:
        console.print("历史结果读取失败：" + str(exc), markup=False)


async def _acknowledge_displayed_history(
    client: GatewayClient, project_id: str, session_id: str, records: list[dict[str, object]],
) -> str | None:
    try:
        await client.acknowledge_session_history(project_id, session_id, records)
    except Exception:
        return "历史已恢复，但 Inbox 已读确认失败；未读记录已保留。"
    return None


def _render_restored_history(records: list[dict[str, object]]) -> None:
    """按正常聊天流重放此前对话，同时隐藏 reasoning 等内部审计字段。"""
    if not records:
        console.print("[dim]该会话当前没有历史记录。[/]")
        return
    console.print("[dim]── 已恢复的对话上下文 ──[/]")
    for record in records:
        role = str(record.get("role", ""))
        content = record.get("content")
        timestamp = str(record.get("timestamp", ""))
        time_suffix = f" [dim]{timestamp}[/]" if timestamp else ""
        if role == "user":
            console.print(f"[bold blue]你 >[/] {str(content or '')}{time_suffix}")
        elif role == "assistant" and content is not None:
            console.print(Panel(
                str(content),
                title="Yuan Ye Agent",
                subtitle=timestamp or None,
                border_style="cyan",
            ))
        elif role == "assistant":
            calls = record.get("tool_calls")
            names = []
            if isinstance(calls, list):
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if isinstance(function, dict) and function.get("name"):
                        names.append(str(function["name"]))
            label = "、".join(names) if names else "未知工具"
            console.print(f"[dim]  工具请求 {label}{time_suffix}[/]")
        elif role == "tool":
            name = str(record.get("name", "tool"))
            status = str(record.get("status", "unknown"))
            style = "green" if status == "success" else "yellow" if status in {"skipped", "cancelled"} else "red"
            console.print(f"[{style}]  工具 {name}：{status}[/{style}]{time_suffix}")
        elif role == "summary":
            console.print(Panel(
                str(content or ""), title="上下文摘要", subtitle=timestamp or None,
                border_style="dim",
            ))
    console.print("[dim]── 继续对话 ──[/]")


def _render_inbox_table(items: list[dict[str, object]], *, unread_only: bool) -> None:
    if not items:
        console.print("暂无未读后台结果。" if unread_only else "Inbox 暂无结果。")
        return
    table = Table(
        title="未读 Inbox" if unread_only else "全部 Inbox",
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("状态")
    table.add_column("任务")
    table.add_column("结果摘要")
    # Timestamps are important metadata and must never be shortened.
    table.add_column("时间", style="dim", min_width=25, no_wrap=True)
    table.add_column("已读", justify="center")
    for item in items:
        item_id = str(item.get("item_id", ""))
        summary = str(item.get("summary", ""))
        table.add_row(
            item_id[:12],
            str(item.get("status", "")),
            str(item.get("title", "")),
            summary if len(summary) <= 80 else summary[:77] + "…",
            str(item.get("created_at", "")),
            "是" if bool(item.get("read")) else "否",
        )
    console.print(table)
    console.print("[dim]可使用表格中的 ID 前缀执行 /inbox show 或 /inbox read。[/]")


def _render_skill_catalog(rows: list[tuple[str, str, str]]) -> None:
    """Render the full audited Skill catalog with Rich's native grid."""

    table = Table(
        title="已审核 Skill",
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    table.add_column("名称", style="cyan")
    table.add_column("描述")
    table.add_column("位置")
    for name, description, location in rows:
        table.add_row(name, description, location)
    console.print(table)


def _render_inbox_item(item: dict[str, object]) -> None:
    content = (
        f"状态：{item.get('status', '')}\n"
        f"任务：{item.get('title', '')}\n"
        f"结果：{item.get('summary', '') or '（无结果正文）'}\n"
        f"Session：{item.get('session_id') or '-'}\n"
        f"Run：{item.get('run_id', '')}\n"
        f"Project：{item.get('project_id', '')}\n"
        f"时间：{item.get('created_at', '')}\n"
        f"已读：{'是' if bool(item.get('read')) else '否'}"
    )
    console.print(Panel(content, title=f"Inbox {item.get('item_id', '')}"))


async def _code_mode(
    client: GatewayClient, project_id: str, origin_session_id: str | None = None,
) -> None:
    """在 Gateway 托管的隔离 worktree 中运行持续 Extension Coding 会话。"""
    try:
        with console.status("[cyan]正在创建隔离 Git worktree 和 Coding Runtime…[/]"):
            session = await client.start_code_session(project_id, origin_session_id)
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
                if result.status == "capability_confirmation_required":
                    plan = result.grant_plan
                    lines = []
                    for hook in plan.get("hooks", []):
                        controlled = hook.get("confirmation_required_capabilities", [])
                        tools = [item.get("name") for item in hook.get("tools", [])]
                        if controlled or tools:
                            lines.append(
                                f"{hook.get('hook_id')}: capabilities={controlled or '-'}; "
                                f"tools={tools or '-'}"
                            )
                    console.print(Panel(
                        "\n".join(lines) or "No elevated capabilities",
                        title=f"Extension Candidate Grant {str(plan.get('plan_hash', ''))[:12]}",
                        border_style="yellow",
                    ))
                    if not typer.confirm(
                        "Confirm this exact Candidate grant and continue merge?", default=False,
                    ):
                        console.print("[yellow]Candidate remains unmerged in the /code worktree.[/]")
                        continue
                    result = await client.finalize_code_session(
                        session.code_session_id, str(plan["plan_hash"]),
                    )
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
    session_id: str | None,
    task: str,
    *,
    confirm=None,
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
            _render_skill_catalog([
                (str(item["name"]), str(item["description"]), str(item["location"]))
                for item in catalog
            ])
            return
        if action == "refresh":
            if not session_id:
                raise ValueError("当前没有活动 Session；先发送一条消息，再执行 /skill refresh")
            with console.status(
                "[cyan]正在恢复 Session、检查 Skill 并按需切换上下文分段…[/]",
            ):
                result = await client.refresh_skills(project_id, session_id)
            style = "green" if result.get("status") in {"refreshed", "unchanged"} else "red"
            console.print(f"[{style}]{result.get('message', 'Skill 刷新完成')}[/]")
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
        update_confirmed = False
        if action == "update":
            update_confirmed = (
                await confirm("更新 Skill", "将替换现有 Skill，是否继续？")
                if confirm is not None
                else typer.confirm("更新会替换现有 Skill，是否继续？", default=False)
            )
        payload = {
            "project_id": project_id,
            "action": action,
            "source": parts[position],
            "name": name,
            "ref": options.get("ref"),
            "skill_path": options.get("skill_path"),
            "confirmed": update_confirmed,
        }
        if action == "update" and not payload["confirmed"]:
            console.print("[yellow]已取消 Skill 更新。[/]")
            return
        result = await client.manage_skill(payload)
        accept_risk = False
        if result["status"] == "declined":
            message = f"{result['message']} 是否接受审核报告中的风险并重新安装？"
            accept_risk = (
                await confirm("Skill 风险确认", message)
                if confirm is not None
                else typer.confirm(message, default=False)
            )
        if accept_risk:
            payload["confirmed"] = True
            result = await client.manage_skill(payload)
        style = "green" if result["status"] == "installed" else "yellow"
        console.print(f"[{style}]{result['message']}[/]")
        if result.get("report_path"):
            console.print(f"[dim]审核报告：{result['report_path']}[/]")
    except Exception as exc:
        console.print(f"[red]{str(exc) or type(exc).__name__}[/]")


async def _handle_dream_command(client: GatewayClient, task: str) -> None:
    """管理每日全局 Profile 巩固，不写入普通 Session。"""
    try:
        parts = shlex.split(task)
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action == "status" and len(parts) in {1, 2}:
            status = await client.dream_status()
            console.print(Panel(
                f"启用：{'是' if status.enabled else '否'}\n"
                f"运行中：{'是' if status.running else '否'}\n"
                f"计划：{status.schedule}（{status.timezone}）\n"
                f"下次运行：{status.next_run_at or '-'}\n"
                f"最近完成日期：{status.last_completed_date or '-'}\n"
                f"最近状态：{status.last_status or '-'}\n"
                f"最近错误：{status.last_error or '-'}",
                title="Dream 状态",
            ))
            return
        if action == "run" and len(parts) in {2, 3}:
            result = await client.run_dream(parts[2] if len(parts) == 3 else None)
            _render_dream_result(result)
            return
        if action == "backfill" and len(parts) == 4:
            results = await client.backfill_dream(parts[2], parts[3])
            for result in results:
                _render_dream_result(result)
            return
        if action == "rollback" and len(parts) in {2, 3}:
            result = await client.rollback_dream(parts[2] if len(parts) == 3 else None)
            style = "green" if result.restored else "yellow"
            console.print(f"[{style}]{result.message}[/]")
            return
        raise ValueError(
            "用法：/dream status；/dream run [YYYY-MM-DD]；"
            "/dream backfill <开始日期> <结束日期>；/dream rollback [run-id]"
        )
    except Exception as exc:
        console.print(f"[red]{str(exc) or type(exc).__name__}[/]")


async def _handle_harness_command(
    client: GatewayClient, task: str, *, confirm=None,
) -> None:
    """Handle durable Harness Dream commands outside the chat transcript."""
    try:
        parts = shlex.split(task)
        if len(parts) < 2 or parts[1].lower() != "dream":
            raise ValueError("用法：/harness dream status|run|freeze|unfreeze|revert")
        action = parts[2].lower() if len(parts) > 2 else "status"
        if action == "status" and len(parts) == 3:
            console.print_json(data=await client.harness_dream_status())
            return
        if action == "run" and len(parts) in {3, 4}:
            selected = parts[3] if len(parts) == 4 else None
            approved = (
                await confirm(
                    "Harness Dream",
                    "将修改 Yuan Ye 源码并在隔离环境验证，是否运行？",
                )
                if confirm is not None
                else typer.confirm("Harness Dream 会修改 Yuan Ye 源码，确认运行？", default=False)
            )
            if not approved:
                console.print("[yellow]已取消[/]")
                return
            console.print_json(data=await client.run_harness_dream(selected))
            return
        if action == "freeze":
            reason = " ".join(parts[3:]).strip() or "operator freeze"
            console.print_json(data=await client.freeze_harness_dream(reason))
            return
        if action == "unfreeze" and len(parts) == 3:
            console.print_json(data=await client.unfreeze_harness_dream())
            return
        if action == "revert" and len(parts) == 4:
            approved = (
                await confirm(
                    "Harness Dream 回滚",
                    "仅生成回滚候选，不会立即合并。是否继续？",
                )
                if confirm is not None
                else typer.confirm("仅生成回滚候选，不会立即合并。继续？", default=False)
            )
            if not approved:
                console.print("[yellow]已取消[/]")
                return
            console.print_json(data=await client.create_harness_dream_revert(parts[3]))
            return
        raise ValueError(
            "用法：/harness dream status；/harness dream run [YYYY-MM-DD|operation_id]；"
            "/harness dream freeze [reason]；/harness dream unfreeze；"
            "/harness dream revert <operation_id>"
        )
    except Exception as exc:
        console.print(f"[red]{str(exc) or type(exc).__name__}[/]")


def _render_dream_result(result) -> None:
    style = "green" if result.status == "completed" else "yellow" if result.status == "noop" else "red"
    console.print(Panel(
        f"{result.message}\n日期：{result.date}\n运行：{result.run_id}\n"
        f"Session：{result.sessions_processed}；证据：{result.evidence_processed}；"
        f"记忆变更：{result.memories_changed}",
        title="Dream",
        border_style=style,
    ))


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
            _render_skill_catalog([
                (item.name, item.description, item.location) for item in catalog
            ])
            return
        if action == "refresh":
            if len(parts) != 2:
                raise ValueError("用法：/skill refresh")
            result = await runtime.refresh_skills(runtime.active_session_id)
            style = "green" if result.status in {"refreshed", "unchanged"} else "red"
            console.print(f"[{style}]{result.message}[/]")
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
    """Legacy local runtime hook; Gateway is the sole ERROR Evolution owner."""
    del config, runtime, task, session_id
    if failure.snapshot_worthy:
        console.print(
            "[yellow]本地 Runtime 不再直接执行 Harness；请通过 Gateway 对话重试，"
            "Gateway 会保存完整 RuntimeFailure 并发出一次确认 Proposal。[/]"
        )


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
    async def show() -> None:
        client = _gateway_client()
        project = await _gateway_project(client)
        project_id = str(project["project_id"])
        records = await client.session(project_id, session_id)
        table = Table(title=f"会话 {session_id}")
        table.add_column("时间", style="dim")
        table.add_column("角色", style="cyan")
        table.add_column("内容")
        for record in records:
            table.add_row(str(record.get("timestamp", "")), str(record.get("role", "")), str(record.get("content", "")))
        console.print(table)
        warning = await _acknowledge_displayed_history(
            client, project_id, session_id, records,
        )
        if warning:
            console.print(f"[yellow]{warning}[/]")

    asyncio.run(show())


@gateway_app.command("start")
def gateway_start(port: int | None = typer.Option(None, "--port")) -> None:
    """启动单实例本机 Gateway。"""
    config = load_runtime_config()
    manager = GatewayProcessManager(config.agent_root, port or config.gateway_port)
    status = manager.ensure_running()
    console.print(f"[green]Gateway 已运行[/] PID={status['pid']} {status['base_url']}")


@gateway_app.command("stop")
def gateway_stop(port: int | None = typer.Option(None, "--port"),
                 timeout: float = typer.Option(30, "--timeout", min=1, max=3600)) -> None:
    """停止当前 Agent Home 的 Gateway。"""
    config = load_runtime_config()
    manager = GatewayProcessManager(config.agent_root, port or config.gateway_port)
    try:
        stopped = manager.stop(
            timeout_seconds=timeout,
            maintenance_wait_seconds=config.backup_drain_timeout_seconds,
        )
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=1) from exc
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
    if status.get("maintenance"):
        console.print(status["maintenance"])


@gateway_app.command("quiesce")
def gateway_quiesce(timeout: float = typer.Option(30, min=1, max=3600),
                    reason: str = typer.Option("operator maintenance")) -> None:
    """关闭新工作准入，等待现有工作安全完成；超时不强杀。"""
    config = load_runtime_config()
    client = GatewayClient(config.agent_root, port=config.gateway_port)
    console.print(asyncio.run(client.quiesce(timeout, reason)))


@gateway_app.command("resume")
def gateway_resume(epoch: int = typer.Option(..., min=1), revision: int = typer.Option(..., min=0)) -> None:
    """按精确 maintenance epoch/revision 健康检查后恢复。"""
    config = load_runtime_config()
    # Resume is a control-plane command whose purpose is to make a healthy but
    # non-accepting Gateway accept work again. Requiring normal work readiness
    # in GatewayClient.__init__ makes that recovery path self-deadlock.
    client = GatewayClient(
        config.agent_root,
        port=config.gateway_port,
        auto_start=False,
    )
    console.print(asyncio.run(client.resume(epoch, revision)))


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


@cron_app.command("list")
def cron_list() -> None:
    """列出当前 workspace 的全部 Cron Job。"""
    async def execute():
        client = _gateway_client()
        project = await _gateway_project(client)
        return await client.cron_jobs(str(project["project_id"]))
    _render_cron_jobs(asyncio.run(execute()))


@cron_app.command("status")
def cron_status() -> None:
    """显示 Heartbeat 健康状态和任务计数。"""
    async def execute():
        return await _gateway_client().cron_status()
    console.print(asyncio.run(execute()).model_dump_json(indent=2))


@cron_app.command("preview")
def cron_preview(
    expression: str,
    timezone_name: str = typer.Option(None, "--timezone"),
    count: int = typer.Option(5, "--count", min=1, max=20),
) -> None:
    """预览五段 Cron 表达式的未来执行时间（持久化值为 UTC）。"""
    zone = timezone_name or CronScheduleCalculator.local_timezone()
    async def execute():
        return await _gateway_client().cron_preview(
            CronSchedule(kind="cron", expression=expression, timezone=zone), count,
        )
    result = asyncio.run(execute())
    console.print("\n".join(_cron_preview_local(result)) or "没有后续执行时间")


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name"),
    prompt: str = typer.Option(..., "--prompt"),
    every: str | None = typer.Option(None, "--every"),
    expression: str | None = typer.Option(None, "--cron"),
    timezone_name: str | None = typer.Option(None, "--timezone"),
) -> None:
    """创建固定间隔或五段 Cron 任务。"""
    if bool(every) == bool(expression):
        raise typer.BadParameter("必须且只能提供 --every 或 --cron")
    schedule = (
        CronSchedule(kind="interval", interval_seconds=_parse_interval(str(every)))
        if every else CronSchedule(
            kind="cron",
            expression=expression,
            timezone=timezone_name or CronScheduleCalculator.local_timezone(),
        )
    )
    async def execute():
        client = _gateway_client()
        project = await _gateway_project(client)
        return await client.create_cron(CronJobCreateRequest(
            project_id=str(project["project_id"]), name=name, prompt=prompt, schedule=schedule,
        ))
    result = asyncio.run(execute())
    console.print(f"[green]已创建[/] {result.job_id}，下次：{result.next_run_at}")


@cron_app.command("init-paper-research")
def cron_init_paper_research(
    expression: str = typer.Option("0 9 * * 1", "--cron"),
    timezone_name: str | None = typer.Option(None, "--timezone"),
) -> None:
    """幂等创建内置的无人值守论文调研计划。"""
    result = asyncio.run(_create_initial_paper_research_cron(
        expression,
        timezone_name or CronScheduleCalculator.local_timezone(),
    ))
    console.print(
        f"[green]论文调研 Cron 已就绪[/] {result.job_id}，"
        f"下次：{result.next_run_at}"
    )


@cron_app.command("at")
def cron_at(
    when: str,
    name: str = typer.Option(..., "--name"),
    prompt: str = typer.Option(..., "--prompt"),
) -> None:
    """创建一次性、带时区的 ISO 8601 任务。"""
    async def execute():
        client = _gateway_client()
        project = await _gateway_project(client)
        return await client.create_cron(CronJobCreateRequest(
            project_id=str(project["project_id"]), name=name, prompt=prompt,
            schedule=CronSchedule(kind="once", run_at=when),
        ))
    result = asyncio.run(execute())
    console.print(f"[green]已创建[/] {result.job_id}，执行时间：{result.next_run_at}")


@cron_app.command("edit")
def cron_edit(
    job_id: str,
    name: str | None = typer.Option(None, "--name"),
    prompt: str | None = typer.Option(None, "--prompt"),
    every: str | None = typer.Option(None, "--every"),
    expression: str | None = typer.Option(None, "--cron"),
    timezone_name: str | None = typer.Option(None, "--timezone"),
) -> None:
    """编辑任务名称、Prompt 或计划。"""
    if every and expression:
        raise typer.BadParameter("--every 与 --cron 不能同时提供")
    schedule = None
    if every:
        schedule = CronSchedule(kind="interval", interval_seconds=_parse_interval(every))
    elif expression:
        schedule = CronSchedule(
            kind="cron", expression=expression,
            timezone=timezone_name or CronScheduleCalculator.local_timezone(),
        )
    if name is None and prompt is None and schedule is None:
        raise typer.BadParameter("至少提供一项修改")
    async def execute():
        return await _gateway_client().edit_cron(
            job_id, CronJobEditRequest(name=name, prompt=prompt, schedule=schedule),
        )
    console.print(f"[green]已更新[/] {asyncio.run(execute()).job_id}")


def _cron_action_command(job_id: str, action: str) -> None:
    async def execute():
        return await getattr(_gateway_client(), f"{action}_cron")(job_id)
    result = asyncio.run(execute())
    console.print(f"[green]{action} 完成[/] {result.job_id}")


@cron_app.command("pause")
def cron_pause(job_id: str) -> None:
    _cron_action_command(job_id, "pause")


@cron_app.command("resume")
def cron_resume(job_id: str) -> None:
    _cron_action_command(job_id, "resume")


@cron_app.command("run")
def cron_run(job_id: str) -> None:
    _cron_action_command(job_id, "run")


@cron_app.command("remove")
def cron_remove(job_id: str) -> None:
    _cron_action_command(job_id, "remove")


def _parse_interval(value: str) -> int:
    import re
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", value, re.IGNORECASE)
    if match is None:
        raise ValueError("固定间隔应类似 30m、2h、1d 或直接填写秒数")
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2).lower()]
    seconds = amount * multiplier
    if seconds < 60:
        raise ValueError("固定间隔不能小于 60 秒")
    return seconds


def _render_cron_jobs(jobs) -> None:
    if not jobs:
        console.print("当前项目没有 Cron Job。")
        return
    table = Table(title="Cron Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("名称")
    table.add_column("状态")
    table.add_column("计划")
    table.add_column("下次执行（任务时区）")
    table.add_column("运行/失败/重叠")
    for job in jobs:
        schedule = (
            f"every {job.schedule.interval_seconds}s" if job.schedule.kind == "interval"
            else str(job.schedule.run_at) if job.schedule.kind == "once"
            else f"{job.schedule.expression} [{job.schedule.timezone}]"
        )
        table.add_row(
            job.job_id, job.name, job.state, schedule, _cron_local_time(job),
            f"{job.run_count}/{job.failure_count}/{job.skipped_overlap_count}",
        )
    console.print(table)


def _cron_local_time(job) -> str:
    if not job.next_run_at:
        return "-"
    from datetime import datetime
    from zoneinfo import ZoneInfo
    selected = datetime.fromisoformat(job.next_run_at.replace("Z", "+00:00"))
    return selected.astimezone(ZoneInfo(job.schedule.timezone)).isoformat(timespec="seconds")


def _cron_preview_local(preview) -> tuple[str, ...]:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(preview.schedule.timezone)
    return tuple(
        datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone).isoformat(timespec="seconds")
        for value in preview.next_runs
    )


@backup_app.command("create")
def backup_create(
    output: Path | None = typer.Option(None, "--output", help="输出 .manifest 路径"),
    manual_passphrase: bool = typer.Option(
        False,
        "--manual-passphrase",
        help="使用手动口令创建可跨设备恢复的备份",
    ),
) -> None:
    """默认使用系统托管密钥；可显式选择手动口令。"""
    config = load_runtime_config()
    first: str | None = None
    if manual_passphrase or config.backup_key_mode == "passphrase":
        first = getpass.getpass("Backup 口令: ")
        second = getpass.getpass("再次输入口令: ")
        if not first or first != second:
            raise typer.BadParameter("两次口令不一致或为空")
    record = asyncio.run(_gateway_client().create_backup(first, output))
    console.print(f"[green]Snapshot Backup 完成[/] {record.path}")
    console.print(
        f"backup_id={record.backup_id} size={record.size_bytes} "
        f"encryption={record.encryption_mode}",
    )


@backup_app.command("list")
def backup_list() -> None:
    """列出当前备份目录中的已发布 Snapshot Manifest。"""
    records = asyncio.run(_gateway_client().backups())
    if not records:
        console.print("没有备份。")
        return
    for record in records:
        console.print(f"{record.created_at.isoformat()}  {record.size_bytes:>12}  {record.path}")


@backup_app.command("status")
def backup_status() -> None:
    """显示备份计划与当前维护状态。"""
    status = asyncio.run(_gateway_client().backup_status())
    console.print_json(data=status)


@backup_app.command("verify")
def backup_verify(archive: Path) -> None:
    """验证Manifest、加密对象、文件哈希和SQLite一致性。"""
    service = BackupService(default_agent_root())
    if archive.suffix == ".manifest":
        manifest = read_manifest(archive)
        password = getpass.getpass("Backup 口令: ") if manifest.encryption_mode == "passphrase" else None
    else:
        header = EncryptedBackupArchive.read_header(archive)
        password = getpass.getpass("Backup 口令: ") if header.key_mode == "passphrase" else None
    result = service.verify(archive, password)
    console.print_json(data=result.model_dump(mode="json"))
    if not result.valid:
        raise typer.Exit(1)


@backup_app.command("restore")
def backup_restore(
    archive: Path,
    map_path: list[str] = typer.Option([], "--map-path", help="仅映射Manifest外部依赖：OLD=NEW"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    confirm_backup_id: str | None = typer.Option(None, "--confirm-backup-id"),
) -> None:
    """停止Gateway，创建救援备份，然后整体替换Agent Home。"""
    root = default_agent_root()
    service = BackupService(root)
    if archive.suffix == ".manifest":
        manifest = read_manifest(archive)
        entered = getpass.getpass("Backup 口令: ") if manifest.encryption_mode == "passphrase" else None
    else:
        header = EncryptedBackupArchive.read_header(archive)
        entered = getpass.getpass("Backup 口令: ") if header.key_mode == "passphrase" else None
    password = service.resolve_archive_secret(archive, entered)
    mappings: dict[str, str] = {}
    for item in map_path:
        if "=" not in item:
            raise typer.BadParameter("--map-path 必须使用 OLD=NEW")
        old, new = item.split("=", 1)
        if not old or not new or old in mappings:
            raise typer.BadParameter("--map-path 不能为空或重复")
        mappings[old] = new
    restore = RestoreService(root, service)
    plan = restore.plan(archive, password, mappings)
    console.print(
        f"backup_id={plan.backup_id}\ncreated={plan.created_at.isoformat()}\n"
        f"agent={plan.agent_version}\narchive={plan.archive_size} bytes\n"
        f"logical={plan.logical_size} bytes\npeak={plan.estimated_peak_bytes} bytes\n"
        f"available={plan.available_bytes} bytes",
    )
    expected = plan.backup_id[:8]
    confirmation = confirm_backup_id if non_interactive else typer.prompt(
        f"输入备份短ID {expected} 确认破坏性Restore",
    )
    if not confirmation:
        raise typer.BadParameter("缺少精确备份ID确认")
    manager = GatewayProcessManager(root)
    if manager.status().get("running"):
        manager.stop()
    restore_id = asyncio.run(restore.restore(
        archive, password,
        confirmation=confirmation,
        non_interactive=non_interactive,
        path_mappings=mappings,
    ))
    console.print(f"[green]Restore 已提交[/] restore_id={restore_id}")


@backup_app.command("recover")
def backup_recover() -> None:
    """按外部Journal协调未完成Restore，不猜测目录阶段。"""
    root = default_agent_root()
    state = asyncio.run(RestoreService(root, BackupService(root)).recover_interrupted_restore())
    console.print(f"Restore recovery state: {state.value}")


@backup_app.command("rollback")
def backup_rollback() -> None:
    """回滚当前Fence指向的未完成Restore。"""
    root = default_agent_root()
    state = asyncio.run(RestoreService(root, BackupService(root)).rollback())
    console.print(f"Restore rollback state: {state.value}")


@backup_app.command("prune")
def backup_prune() -> None:
    """清理超过27天的Manifest并回收没有Manifest引用的对象。"""
    config = load_runtime_config()
    service = BackupService(
        config.agent_root,
        backup_directory=config.backup_directory,
        retention_days=config.backup_retention_days,
        min_free_space_bytes=config.backup_min_free_space_bytes,
        max_storage_bytes=config.backup_max_storage_bytes,
    )
    removed = service.apply_retention()
    console.print(f"已清理 {len(removed)} 个超过27天或超出空间上限的Snapshot Manifest，并回收无引用对象。")


@app.command()
def serve_ui(port: int | None = typer.Option(None, "--port")) -> None:
    """自动启动 Gateway 并打开本机 Web 工作台。"""
    serve(port)


def main() -> None:
    """供源码入口和打包命令调用。"""
    arguments = sys.argv[1:]
    restore_control = (
        len(arguments) >= 2
        and arguments[0] == "backup"
        and arguments[1] in {"verify", "restore", "recover", "rollback"}
    )
    if (not arguments or arguments[0] != "init") and not restore_control:
        result = ensure_project_initialized(prepare_default_agent_root())
        if result.initialized:
            console.print(f"[green]首次运行初始化完成[/] {result.yy_dir}")
            console.print(f"请按需编辑 {result.yy_dir / 'settings.local.json'}；后续启动不会重复初始化。")
        if result.initialized:
            _offer_initial_paper_research_cron(result.yy_dir)
    app()
