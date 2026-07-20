"""实时 Rich CLI：仅消费 AgentRuntime 事件。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from Agent import AgentRuntime, EventType, load_runtime_config
from bootstrap import ensure_project_initialized, initialize_project
from memory import MemoryStore
from .web import serve

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Yuan Ye Study Agent 本地入口")
session_app = typer.Typer(help="列出、查看和恢复本地会话")
app.add_typer(session_app, name="session")
console = Console()


@app.command()
def init() -> None:
    """初始化本机 `.yy` 配置、会话索引和长期记忆文件。"""
    yy = initialize_project(Path.cwd())
    console.print(f"[green]初始化完成[/] {yy}")
    console.print("请编辑 .yy/settings.local.json 配置模型；已有文件不会被覆盖。")


def _memory() -> MemoryStore:
    """从当前项目配置创建 Memory 门面。"""
    return MemoryStore(load_runtime_config().memory_dir)


def _validate_session(session_id: str | None) -> str | None:
    """校验可选会话哈希，避免恢复命令在模型调用后才失败。"""
    if session_id and not _memory().has_session(session_id):
        raise typer.BadParameter(f"未找到会话：{session_id}", param_hint="--session")
    return session_id


async def _approve(name: str, arguments: dict[str, object]) -> bool:
    """在终端中请求一次性高风险工具批准。"""
    return typer.confirm(f"允许执行 {name} {arguments}？", default=False)


async def _render(runtime: AgentRuntime, task: str, session_id: str | None = None) -> str:
    """边接收事件边刷新面板，避免模型等待期间终端静止。"""
    lines: list[str] = []
    streaming_text = ""
    displayed_status = ""
    active_session_id = session_id or ""
    try:
        with Live(Panel("正在准备…", title="Yuan Ye Agent"), console=console, refresh_per_second=10) as live:
            async for event in runtime.run_task(task, session_id):
                if event.type is EventType.STARTED:
                    active_session_id = str(event.payload["session_id"])
                elif event.type is EventType.TEXT:
                    streaming_text += str(event.payload["content"])
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
    except Exception as exc:
        console.print(Panel(f"[red]{str(exc) or type(exc).__name__}[/]", title="Yuan Ye Agent 运行错误"))
    return active_session_id


async def _run_once(task: str, session_id: str | None) -> str:
    """为单次任务创建并可靠关闭一个 Session 运行范围。"""
    runtime = AgentRuntime(approval=_approve)
    try:
        return await _render(runtime, task, session_id)
    finally:
        await runtime.close()


@app.command()
def run(task: str, session_id: str | None = typer.Option(None, "--session", "-s", help="继续指定会话哈希")) -> None:
    """运行一次任务。"""
    session_id = _validate_session(session_id)
    try:
        active_id = asyncio.run(_run_once(task, session_id))
    except Exception as exc:
        console.print(Panel(f"[red]{str(exc) or type(exc).__name__}[/]", title="Yuan Ye Agent 配置错误"))
        return
    if active_id:
        console.print(f"[dim]会话哈希：{active_id}[/]")


@app.command()
def chat(session_id: str | None = typer.Option(None, "--session", "-s", help="恢复指定会话哈希")) -> None:
    """启动连续交互会话。"""
    session_id = _validate_session(session_id)
    try:
        asyncio.run(_chat(session_id))
    except Exception as exc:
        console.print(Panel(f"[red]{str(exc) or type(exc).__name__}[/]", title="Yuan Ye Agent 配置错误"))


async def _chat(session_id: str | None) -> None:
    """在一个 Runtime/Session 中处理多次用户输入，退出时触发 trace_end。"""
    console.print("[bold cyan]Yuan Ye Agent[/]  输入 /help 查看命令，/exit 退出。")
    if session_id:
        console.print(f"[green]已恢复会话[/] {session_id}（{len(_memory().session_records(session_id))} 条消息）")
    runtime = AgentRuntime(approval=_approve)
    try:
        while True:
            task = console.input("[bold blue]你 > [/]").strip()
            if task in {"/exit", "/quit"}:
                return
            if task == "/help":
                console.print("/compress 压缩当前上下文；/exit 退出；其余内容将发送给 Agent。")
                continue
            if task:
                previous_id = session_id
                session_id = await _render(runtime, task, session_id)
                if session_id and not previous_id:
                    console.print(f"[dim]会话哈希：{session_id}；下次可使用 chat --session {session_id} 恢复[/]")
    finally:
        await runtime.close()


@session_app.command("list")
def session_list() -> None:
    """列出可恢复会话。"""
    sessions = _memory().list_sessions()
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
    """显示指定会话最新分段中的带时间戳消息。"""
    _validate_session(session_id)
    table = Table(title=f"会话 {session_id}")
    table.add_column("时间", style="dim")
    table.add_column("角色", style="cyan")
    table.add_column("内容")
    for record in _memory().session_records(session_id):
        table.add_row(str(record.get("timestamp", "")), str(record.get("role", "")), str(record.get("content", "")))
    console.print(table)


@app.command()
def serve_ui(port: int = typer.Option(8765, "--port")) -> None:
    """启动仅绑定回环地址的 Web UI。"""
    serve(port)


def main() -> None:
    """供源码入口和打包命令调用。"""
    if not sys.argv[1:] or sys.argv[1] != "init":
        result = ensure_project_initialized(Path.cwd())
        if result.initialized:
            console.print(f"[green]首次运行初始化完成[/] {result.yy_dir}")
            console.print("请按需编辑 .yy/settings.local.json；后续启动不会重复初始化。")
    app()
