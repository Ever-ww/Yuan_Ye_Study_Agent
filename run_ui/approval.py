"""Claude Code 风格的终端危险操作选择器。"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from enum import Enum
from typing import Any

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


class ApprovalChoice(str, Enum):
    """一次危险操作可选择的授权范围。"""

    ONCE = "once"
    SESSION = "session"
    DENY = "deny"
    TIMEOUT = "timeout"


active_live: ContextVar[Live | None] = ContextVar("yy_active_live", default=None)
KeyReader = Callable[[], str]


class InteractiveApproval:
    """方向键选择审批，并在当前 Runtime 生命周期缓存工具级授权。"""

    def __init__(
        self,
        console: Console,
        *,
        key_reader: KeyReader | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.console = console
        self.key_reader = key_reader
        self.timeout_seconds = max(1, timeout_seconds)
        self.session_allowed_tools: set[str] = set()
        self.last_timed_out = False

    async def __call__(self, name: str, arguments: dict[str, Any]) -> bool:
        self.last_timed_out = False
        if name in self.session_allowed_tools:
            self.console.print(f"[dim]已按当前会话授权执行 {name}。[/]")
            return True
        outer_live = active_live.get()
        if outer_live is not None:
            outer_live.stop()
        try:
            choice = self._choose(name, arguments)
        except (typer.Abort, EOFError, KeyboardInterrupt):
            choice = ApprovalChoice.DENY
        finally:
            if outer_live is not None:
                outer_live.start(refresh=True)
        self.last_timed_out = choice is ApprovalChoice.TIMEOUT
        if choice is ApprovalChoice.SESSION:
            self.session_allowed_tools.add(name)
            self.console.print(f"[yellow]当前会话后续的 {name} 将不再询问。[/]")
        elif choice in {ApprovalChoice.DENY, ApprovalChoice.TIMEOUT}:
            self.console.print("[yellow]已拒绝本次工具操作。[/]")
        return choice in {ApprovalChoice.ONCE, ApprovalChoice.SESSION}

    def _choose(self, name: str, arguments: dict[str, Any]) -> ApprovalChoice:
        if self.key_reader is None and not (self.console.is_terminal and sys.stdin.isatty()):
            return ApprovalChoice.ONCE if typer.confirm(
                f"允许执行 {name} {arguments}？", default=True,
            ) else ApprovalChoice.DENY
        choices = (ApprovalChoice.ONCE, ApprovalChoice.SESSION, ApprovalChoice.DENY)
        selected = 0
        deadline = time.monotonic() + self.timeout_seconds
        with Live(
            _approval_panel(name, arguments, choices, selected, self.timeout_seconds),
            console=self.console,
            refresh_per_second=20,
            transient=True,
        ) as menu:
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    return ApprovalChoice.TIMEOUT
                key = self.key_reader() if self.key_reader is not None else _read_key(remaining)
                if key == "up":
                    selected = (selected - 1) % len(choices)
                elif key == "down":
                    selected = (selected + 1) % len(choices)
                elif key == "enter":
                    return choices[selected]
                elif key == "timeout":
                    return ApprovalChoice.TIMEOUT
                elif key in {"escape", "ctrl_c"}:
                    return ApprovalChoice.DENY
                menu.update(
                    _approval_panel(name, arguments, choices, selected, int(remaining)),
                    refresh=True,
                )


def _approval_panel(
    name: str,
    arguments: dict[str, Any],
    choices: tuple[ApprovalChoice, ...],
    selected: int,
    remaining_seconds: int,
) -> Panel:
    labels = {
        ApprovalChoice.ONCE: "允许本次操作",
        ApprovalChoice.SESSION: f"当前会话始终允许 {name}",
        ApprovalChoice.DENY: "拒绝",
    }
    body = Text()
    body.append(f"工具：{name}\n", style="bold")
    body.append("参数：\n", style="bold")
    body.append(_arguments_preview(arguments) + "\n\n", style="dim")
    for index, choice in enumerate(choices):
        marker = "❯ " if index == selected else "  "
        style = "bold cyan" if index == selected else "white"
        body.append(f"{marker}{labels[choice]}\n", style=style)
    body.append("\n↑/↓ 选择　Enter 确认　Esc 取消", style="dim")
    body.append(f"\n{remaining_seconds} 秒无操作将自动拒绝", style="bold yellow")
    return Panel(body, title="危险操作确认", border_style="yellow")


def _arguments_preview(
    arguments: dict[str, Any],
    *,
    max_lines: int = 5,
    max_line_chars: int = 88,
) -> str:
    """生成紧凑审批摘要；真实参数仍由 Gateway 完整持久化。"""
    raw_lines = json.dumps(arguments, ensure_ascii=False, indent=2, default=str).splitlines()
    lines = [
        line if len(line) <= max_line_chars else line[: max_line_chars - 1] + "…"
        for line in raw_lines
    ]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    visible = lines[: max_lines - 1]
    visible.append(f"…（另有 {len(lines) - len(visible)} 行参数已隐藏）")
    return "\n".join(visible)


def _read_key(timeout_seconds: float) -> str:
    """跨平台读取单个方向键，不要求用户额外按 Enter。"""
    if os.name == "nt":
        return _read_windows_key(timeout_seconds)
    return _read_posix_key(timeout_seconds)


def _read_windows_key(timeout_seconds: float) -> str:
    import msvcrt

    deadline = time.monotonic() + timeout_seconds
    while not msvcrt.kbhit():
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(0.05)
    value = msvcrt.getwch()
    if value in {"\x00", "\xe0"}:
        return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "unknown")
    return {"\r": "enter", "\n": "enter", "\x1b": "escape", "\x03": "ctrl_c"}.get(value, "unknown")


def _read_posix_key(timeout_seconds: float) -> str:
    import select
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        if not select.select([sys.stdin], [], [], timeout_seconds)[0]:
            return "timeout"
        value = sys.stdin.read(1)
        if value == "\x1b":
            suffix = ""
            while select.select([sys.stdin], [], [], 0.03)[0] and len(suffix) < 2:
                suffix += sys.stdin.read(1)
            return {"[A": "up", "[B": "down"}.get(suffix, "escape")
        return {"\r": "enter", "\n": "enter", "\x03": "ctrl_c"}.get(value, "unknown")
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
