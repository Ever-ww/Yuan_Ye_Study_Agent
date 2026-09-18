"""Session-level terminal UI backed exclusively by the Gateway client API."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import monotonic
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click, Key, MouseScrollUp, Resize
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Markdown, OptionList, Select, Static
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from Agent import EventType
from Agent.config import REASONING_EFFORTS
from gateway import GatewayClient


ExternalCommand = Callable[[str, str | None], Awaitable[str | None]]
HistoryDisplayed = Callable[[], Awaitable[str | None]]
_COMPOSER_PLACEHOLDER = "输入消息，或使用 /help 查看命令"


COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "显示交互命令"),
    ("/code", "进入 Hook Extension Coding 模式"),
    ("/compress", "立即压缩当前 Session 上下文"),
    ("/context refresh", "刷新当前动态上下文"),
    ("/skill list", "列出当前 Skill"),
    ("/inbox", "查看未读后台结果"),
    ("/tool-result <record_id>", "按需查看完整 Tool 结果"),
    ("/cron status", "查看 Cron 调度状态"),
    ("/dream status", "查看 Dream 状态"),
    ("/harness dream status", "查看 Harness Dream"),
    ("/extension status", "查看 Extension 状态"),
    ("/reload status", "查看 Runtime 热更新状态"),
    ("/exit", "退出当前客户端"),
)


class ConversationTimeline(VerticalScroll):
    """Scrollable transcript with explicit sticky-tail semantics."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.follow_tail = True

    def watch_scroll_y(self, previous: float, current: float) -> None:
        # Textual's watcher updates the scrollbar thumb and invalidates the
        # compositor's visible positions. Without it scroll_y changes while
        # the terminal keeps painting the old answer at the old coordinates.
        super().watch_scroll_y(previous, current)
        # Moving upward is explicit history navigation and suspends following.
        # A downward move that has not reached the *new* bottom may simply be a
        # pending sticky-tail callback racing Markdown layout growth, so it
        # must not disable follow mode. Reaching the real bottom opts back in.
        bottom = max(0, self.max_scroll_y - 1)
        if current >= bottom:
            self.follow_tail = True
        elif current < previous:
            self.follow_tail = False

    def watch_virtual_size(self, previous: Size, current: Size) -> None:
        # Child Markdown and disclosure widgets may settle over several layout
        # passes. Follow the changing bottom itself instead of guessing how
        # many delayed scroll calls are sufficient.
        if self.follow_tail and current.height != previous.height:
            self.call_after_refresh(self.follow_tail_if_enabled)

    def pin_to_tail(self) -> None:
        self.follow_tail = True
        self.scroll_end(animate=False, immediate=True)

    def return_to_live_edge(self) -> None:
        """Pin now and again after the submitted Turn has completed layout."""

        self.pin_to_tail()
        self.call_after_refresh(self.follow_tail_if_enabled)
        self.set_timer(0.05, self.follow_tail_if_enabled)

    def scroll_home(self, *args: Any, **kwargs: Any) -> None:
        # Disable sticky-tail at intent time, before an animated/programmatic
        # scroll produces its first offset update. This fences older deferred
        # tail callbacks from snapping the viewport back down.
        self.follow_tail = False
        super().scroll_home(*args, **kwargs)

    def follow_tail_if_enabled(self) -> None:
        """Honor a deferred tail request only while follow mode is still active.

        Stream rendering schedules work for the next refresh.  A wheel event
        may arrive before that callback runs; re-checking here prevents an old
        callback from snapping the user straight back to the bottom.
        """
        if self.follow_tail:
            # This callback already runs after layout. Do not queue a second
            # unconditional scroll that could outlive a subsequent wheel event.
            self.scroll_end(animate=False, immediate=True)
            self.refresh(layout=True)

    def _on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        # Suspend sticky-tail before Textual applies the wheel delta.  Waiting
        # for ``watch_scroll_y`` is too late when token frames arrive quickly:
        # a frame can otherwise pin the view back to the bottom between the
        # wheel event and the resulting scroll offset update.
        self.follow_tail = False
        super()._on_mouse_scroll_up(event)


class ActivityToggle(Static, can_focus=True):
    """Stable activity control whose label is never animation-driven."""

    BINDINGS = [Binding("enter", "toggle_activity", "展开/收起", show=False)]

    def __init__(self, owner: "LoopActivityCard", label: str) -> None:
        super().__init__(label, classes="activity-toggle")
        self.owner = owner

    async def _on_click(self, event: Click) -> None:
        event.stop()
        self.owner.toggle_details()

    def action_toggle_activity(self) -> None:
        self.owner.toggle_details()


class BrailleSpinner(Static):
    """Small deterministic spinner shared by thinking and Tool execution."""

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(self.FRAMES[0], **kwargs)
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(0.09, self._advance)

    def _advance(self) -> None:
        if not self.display:
            return
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self.update(self.FRAMES[self._frame])


class TurnTraceToggle(Static, can_focus=True):
    """Clickable elapsed-time header for a completed Turn trace."""

    BINDINGS = [Binding("enter", "toggle_trace", "展开/收起", show=False)]

    def __init__(self, owner: "TurnTraceDisclosure") -> None:
        super().__init__("", classes="turn-trace-toggle")
        self.owner = owner

    async def _on_click(self, event: Click) -> None:
        event.stop()
        self.owner.toggle_details()

    def action_toggle_trace(self) -> None:
        self.owner.toggle_details()


class TurnTraceDisclosure(Vertical):
    """Own all transient model text and Tool cards for exactly one Turn."""

    def __init__(self) -> None:
        super().__init__(classes="turn-trace trace-running")
        self.header = TurnTraceToggle(self)
        self.detail = Vertical(classes="turn-trace-detail")
        self._collapsed = False
        self._completed = False
        self._elapsed_label = ""
        self._tool_summary = ""
        self._has_process = True

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.detail

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def complete(
        self,
        elapsed_seconds: float,
        *,
        tool_names: list[str] | tuple[str, ...] = (),
        has_process: bool = True,
    ) -> None:
        self._completed = True
        self.remove_class("trace-running")
        self.add_class("trace-complete")
        self._elapsed_label = f"用时 {_format_elapsed(elapsed_seconds)}"
        self._has_process = has_process
        visible_names = [name for name in tool_names if name]
        unique_names = list(dict.fromkeys(visible_names))
        if len(visible_names) == 1:
            self._tool_summary = f"工具 {unique_names[0]}"
        elif len(visible_names) == 2 and len(unique_names) == 2:
            self._tool_summary = f"工具 {'、'.join(unique_names)}"
        elif visible_names:
            self._tool_summary = f"已执行 {len(visible_names)} 个工具"
        else:
            self._tool_summary = ""
        self.header.display = True
        self._set_collapsed(True)

    def toggle_details(self) -> None:
        if not self._completed or not self._has_process:
            return
        self._set_collapsed(not self._collapsed)
        if not self._collapsed:
            cards = list(self.query(LoopActivityCard))
            if len(cards) == 1 and cards[0].collapsed:
                cards[0].toggle_details()
        self.call_after_refresh(self.header.scroll_visible)

    def _set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.detail.display = not collapsed
        self.set_class(collapsed, "-collapsed")
        if self._completed:
            if not self._has_process:
                self.header.update(self._elapsed_label)
                return
            marker = "查看过程" if collapsed else "收起过程"
            parts = [self._elapsed_label]
            if self._tool_summary:
                parts.append(self._tool_summary)
            parts.append(marker)
            self.header.update("  ·  ".join(parts))


class LoopActivityCard(Vertical):
    """One expandable LLM activity surface per ReAct loop."""

    def __init__(self, loop: int = 0) -> None:
        super().__init__(classes="tool-call activity-active -collapsed")
        self.loop = loop
        self.tools: list[dict[str, Any]] = []
        self.reasoning_summary: str | None = None
        self._mode = "thinking"
        self._title_text = "思考中"
        self._collapsed = True
        self.spinner = BrailleSpinner(classes="activity-spinner")
        self.header = ActivityToggle(self, self._title_text)
        # Tool status strings use square brackets (for example ``[success]``).
        # They are data, not Rich markup.  Treating them as markup caused the
        # status to become a style tag and could make the following result use
        # an unintended/invisible style.
        self.detail = Static("", classes="tool-detail", markup=False)
        self.detail.display = False
        self._render_detail()

    def compose(self) -> ComposeResult:
        with Horizontal(classes="activity-header"):
            yield self.spinner
            yield self.header
        yield self.detail

    @property
    def title(self) -> str:
        return self._title_text

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def toggle_details(self) -> None:
        self._collapsed = not self._collapsed
        self.detail.display = not self._collapsed
        self.set_class(self._collapsed, "-collapsed")
        # Keep the control itself reachable; scrolling the entire expanded
        # card into view may push its header above a short terminal viewport.
        self.call_after_refresh(self.header.scroll_visible)

    def start_tool(self, payload: dict[str, Any]) -> None:
        incoming_loop = int(payload.get("loop") or self.loop or 0)
        if not self.loop:
            self.loop = incoming_loop
        self.tools.append({
            "tool_call_id": str(payload.get("tool_call_id") or ""),
            "position": int(payload.get("position") or len(self.tools)),
            "name": str(payload.get("name") or "tool"),
            "arguments": dict(payload.get("arguments") or {}),
            "execution": str(payload.get("execution") or "serial"),
            "status": "running",
            "result": None,
            "observation_id": None,
        })
        self.show_executing()
        self._render_detail()

    def complete_tool(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("name") or "tool")
        tool_call_id = str(payload.get("tool_call_id") or "")
        position = payload.get("position")
        selected = next((
            item for item in self.tools
            if tool_call_id
            and item.get("tool_call_id") == tool_call_id
            and item["status"] == "running"
        ), None)
        if selected is None and position is not None:
            selected = next((
                item for item in self.tools
                if item.get("position") == int(position)
                and item["status"] == "running"
            ), None)
        if selected is None:
            selected = next((
                item for item in self.tools
                if item["name"] == name and item["status"] == "running"
            ), None)
        if selected is None:
            selected = {
                "tool_call_id": tool_call_id,
                "position": int(position or len(self.tools)),
                "name": name,
                "arguments": {},
                "execution": str(payload.get("execution") or "serial"),
            }
            self.tools.append(selected)
        selected.update({
            "status": str(payload.get("status") or "success"),
            "result": str(payload.get("content") or ""),
            "observation_id": (
                str(payload["observation_id"])
                if payload.get("observation_id") else None
            ),
        })
        if any(item.get("status") == "running" for item in self.tools):
            self.show_executing()
        self._render_detail()

    def show_thinking(self) -> None:
        self._set_active("thinking", "思考中")

    def show_executing(self) -> None:
        names = "、".join(dict.fromkeys(
            str(item["name"]) for item in self.tools
        ))
        self._set_active("executing", f"正在执行工具：「{names}」")

    def set_reasoning_summary(self, content: object) -> None:
        text = str(content or "").strip()
        if text:
            self.reasoning_summary = text
            self._render_detail()

    def append_reasoning(self, content: object) -> None:
        text = str(content or "")
        if text:
            self.reasoning_summary = (self.reasoning_summary or "") + text
            self._render_detail()

    def finish_loop(self) -> None:
        if not self.tools:
            self._mode = "complete"
            self._set_title("本轮过程")
            self.spinner.display = False
            self.remove_class("activity-active")
            self.add_class("activity-complete")
            return
        failed = sum(
            item.get("status") in {"error", "failed"} for item in self.tools
        )
        suffix = f" · {failed} 失败" if failed else ""
        loop = f"Loop {self.loop}" if self.loop else "本轮"
        self._mode = "complete"
        self._set_title(f"✓  {loop} 已执行 {len(self.tools)} 个工具{suffix}")
        self.spinner.display = False
        self.remove_class("activity-active")
        self.add_class("activity-complete")

    def settle_pending_tools(self) -> None:
        """Close any stale visual rows at a durable batch boundary."""
        for item in self.tools:
            if item.get("status") == "running":
                item["status"] = "completed"
        self._render_detail()
        self.show_thinking()

    def _set_active(self, mode: str, label: str) -> None:
        self._mode = mode
        self.spinner.display = True
        self.remove_class("activity-complete")
        self.add_class("activity-active")
        self._set_title(label)

    def _set_title(self, value: str) -> None:
        self._title_text = value
        self.header.update(value)

    def _render_detail(self) -> None:
        sections = [
            "思维链:\n"
            + (
                self.reasoning_summary
                or "当前模型未提供可展示的思维链。"
            )
        ]
        if not self.tools:
            sections.append("本轮 Tool:\n本轮未调用工具")
        for index, item in enumerate(self.tools, start=1):
            arguments = self._bounded_preview(json.dumps(
                item.get("arguments") or {}, ensure_ascii=False,
                indent=2, sort_keys=True,
            ))
            body = [
                f"{index}. {item['name']} [{item.get('status', 'running')}]",
            ]
            if item.get("result") is not None:
                result = self._bounded_preview(str(item.get("result") or "(空)"))
                body.append(f"结果:\n{result}")
            body.extend([
                f"执行模式: {item.get('execution', 'serial')}",
                f"参数:\n{arguments}",
            ])
            if item.get("observation_id"):
                body.append(
                    f"完整记录: /tool-result {item['observation_id']}"
                )
            sections.append("\n".join(body))
        self.detail.update("\n\n".join(sections))

    @staticmethod
    def _bounded_preview(value: str, limit: int = 1600) -> str:
        if len(value) <= limit:
            return value
        head = value[:1000]
        tail = value[-400:]
        omitted = len(value) - len(head) - len(tail)
        return f"{head}\n\n… 已省略 {omitted} 个字符 …\n\n{tail}"


class ConfirmModal(ModalScreen[bool]):
    """Small in-TUI confirmation surface; Escape always means deny."""

    BINDINGS = [Binding("escape", "deny", "拒绝", priority=True)]

    CSS = """
    ConfirmModal { align: center middle; background: $background 70%; }
    #confirm-card {
        width: 76;
        max-width: 85%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #confirm-title { text-style: bold; color: $warning; margin-bottom: 1; }
    #confirm-message { height: auto; max-height: 16; overflow-y: auto; }
    #confirm-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #confirm-actions Button { margin-left: 1; min-width: 12; }
    """

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.modal_title = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-card"):
            yield Static(self.modal_title, id="confirm-title")
            yield Static(self.message, id="confirm-message")
            with Horizontal(id="confirm-actions"):
                yield Button("拒绝", id="deny", variant="default")
                yield Button("允许", id="allow", variant="warning")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_deny(self) -> None:
        self.dismiss(False)


class CorrectionModal(ModalScreen[tuple[str, str | None]]):
    """Observer correction decision with an editable proposal."""

    BINDINGS = [Binding("escape", "reject", "拒绝", priority=True)]

    CSS = """
    CorrectionModal { align: center middle; background: $background 70%; }
    #correction-card {
        width: 88;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #correction-title { height: 2; text-style: bold; color: $warning; }
    #correction-reason { height: auto; max-height: 8; margin-bottom: 1; }
    #correction-prompt { height: 3; border: round $accent; }
    #correction-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #correction-actions Button { margin-left: 1; min-width: 12; }
    """

    def __init__(self, reason: str, proposed_prompt: str) -> None:
        super().__init__()
        self.reason = reason
        self.proposed_prompt = proposed_prompt

    def compose(self) -> ComposeResult:
        with Container(id="correction-card"):
            yield Static("意图偏移确认", id="correction-title")
            yield Static(self.reason or "Observer 建议校正下一步方向。", id="correction-reason")
            yield Input(value=self.proposed_prompt, id="correction-prompt")
            with Horizontal(id="correction-actions"):
                yield Button("拒绝", id="reject")
                yield Button("原样采用", id="adopt", variant="primary")
                yield Button("采用编辑", id="edit", variant="warning")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        action = str(event.button.id)
        if action == "reject":
            self.dismiss(("reject", None))
        elif action == "adopt":
            self.dismiss(("adopt", None))
        else:
            self.dismiss(("edit", self.query_one("#correction-prompt", Input).value))

    def action_reject(self) -> None:
        self.dismiss(("reject", None))


def _format_elapsed(seconds: float) -> str:
    """Render a compact stable duration for the completed-Turn disclosure."""

    total_seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {remaining_seconds}s"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def _cache_values(metric: dict[str, Any]) -> tuple[int, int] | None:
    cache = metric.get("prefix_cache")
    if isinstance(cache, dict) and cache.get("status") == "reported":
        hit = cache.get("hit_tokens")
        total = cache.get("total_tokens")
    else:
        tokens = metric.get("input_tokens")
        if not isinstance(tokens, dict) or tokens.get("cached") is None:
            return None
        hit = tokens.get("cached")
        total = tokens.get("context_total")
    if not isinstance(hit, (int, float)) or not isinstance(total, (int, float)):
        return None
    return max(0, int(hit)), max(0, int(total))


def _cache_status_text(metrics: list[dict[str, Any]]) -> str:
    values = [value for item in metrics if (value := _cache_values(item)) is not None]
    if not values:
        return "缓存 ??.?%"
    hit = sum(item[0] for item in values)
    total = sum(item[1] for item in values)
    ratio = hit / total if total else 0.0
    return f"缓存 {ratio:.1%}"


def _restored_cache_status(records: list[dict[str, Any]]) -> str:
    for record in reversed(records):
        raw_calls = record.get("model_calls")
        if isinstance(raw_calls, list):
            calls = [item for item in raw_calls if isinstance(item, dict)]
        else:
            raw_call = record.get("model_call")
            calls = [raw_call] if isinstance(raw_call, dict) else []
        if calls:
            return _cache_status_text(calls)
    return "缓存 ??.?%"


def _restored_model_name(records: list[dict[str, Any]]) -> str | None:
    for record in reversed(records):
        model = record.get("model")
        if isinstance(model, dict) and model.get("name"):
            return str(model["name"])
    return None


class YuanYeChatApp(App[str | None]):
    """Persistent conversation TUI inspired by Hermes' client/core split."""

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
        layers: base overlay;
    }
    #topbar {
        height: 3;
        padding: 1 2 0 2;
        background: $surface;
        color: $text;
        border-bottom: solid $panel-lighten-2;
    }
    #brand { width: 18; color: $text; text-style: bold; }
    #session-context { width: 1fr; color: $text-muted; }
    #model-switch {
        width: auto;
        min-width: 12;
        max-width: 36;
        height: 1;
        padding: 0 1;
        border: none;
        background: transparent;
        color: $text-muted;
        text-style: none;
    }
    #model-switch:hover,
    #model-switch:focus { color: $text; background: $boost; }
    #reasoning-switch {
        width: 11;
        min-width: 11;
        height: 1;
        margin: 0 1 0 0;
        padding: 0;
        border: none;
        background: transparent;
        color: $text-muted;
    }
    #reasoning-switch > SelectCurrent {
        height: 1;
        min-height: 1;
        padding: 0 1;
        border: none;
        background: transparent;
        color: $text-muted;
    }
    #reasoning-switch:hover > SelectCurrent,
    #reasoning-switch:focus > SelectCurrent {
        border: none;
        background: $boost;
        color: $text;
    }
    #reasoning-switch > SelectOverlay {
        width: 11;
        max-height: 8;
        border: round $panel-lighten-2;
        background: $surface;
        color: $text;
        scrollbar-size-vertical: 1;
    }
    #run-status {
        width: auto;
        min-width: 12;
        padding: 0 1;
        color: $text;
        background: transparent;
        text-align: center;
    }
    #run-status.status-running { color: $text; }
    #run-status.status-complete { color: $text; }
    #run-status.status-error { color: $error; }
    #workspace {
        height: 1fr;
    }
    #timeline {
        width: 1fr;
        padding: 0 2 1 2;
        scrollbar-size-vertical: 1;
    }
    #code-timeline {
        display: none;
        width: 1fr;
        padding: 0 2 1 2;
        scrollbar-size-vertical: 1;
    }
    #data-view {
        display: none;
        width: 1fr;
        padding: 1 2;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    #data-content {
        width: 100%;
        height: auto;
    }
    #observer-pane {
        width: 36;
        min-width: 30;
        padding: 0 1 1 1;
        border-left: solid $panel-lighten-2;
        background: $surface;
        scrollbar-size-vertical: 1;
    }
    #observer-heading {
        height: 1;
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    #observer-caption { height: 1; color: $text-muted; margin-bottom: 1; }
    #observer-progress { height: auto; margin: 0; padding: 0; }
    #observer-progress MarkdownH2 { margin: 0; padding: 0; }
    #observer-progress MarkdownParagraph { margin: 0; padding: 0; }
    #observer-progress MarkdownList { margin: 0; padding: 0; }
    .sender {
        height: 1;
        margin: 1 1 0 1;
        color: $text-muted;
        text-style: bold;
    }
    .user-sender { color: $text-muted; }
    .assistant-sender { color: $text-muted; }
    .user-message {
        height: auto;
        margin: 0 1;
        padding: 1 2;
        background: $surface;
        border-left: solid $panel-lighten-2;
    }
    .assistant-message {
        height: auto;
        margin: 0 1;
        padding: 1 2;
        border-left: solid $panel-lighten-2;
    }
    .turn-execution {
        height: auto;
    }
    .turn-trace {
        height: auto;
        margin: 0 1;
    }
    .turn-trace-toggle {
        display: none;
        height: 1;
        width: 1fr;
        padding: 0 1;
        color: $text-muted;
        background: transparent;
        content-align: left middle;
    }
    .turn-trace-toggle:hover,
    .turn-trace-toggle:focus { color: $text; background: $boost; }
    .turn-trace-detail {
        height: auto;
    }
    .tool-message {
        height: auto;
        margin: 0 3;
        padding: 0 1;
        color: $text-muted;
    }
    .tool-call {
        height: auto;
        margin: 0 2;
        padding: 0;
        color: $text-muted;
        background: $surface;
    }
    .activity-header {
        height: 1;
        width: 100%;
        background: $surface;
    }
    .activity-spinner {
        width: 2;
        height: 1;
        color: $text-muted;
        content-align: center middle;
    }
    .activity-toggle {
        width: 1fr;
        min-width: 1;
        height: 1;
        padding: 0;
        border: none;
        background: transparent;
        color: $text-muted;
        text-style: none;
        content-align: left middle;
    }
    .activity-toggle:hover,
    .activity-toggle:focus { color: $text; background: $boost; }
    .tool-detail {
        height: auto;
        margin: 0 1 1 2;
        padding: 1;
        color: $text;
        background: $boost;
        border-left: solid $panel-lighten-2;
    }
    .error-message { color: $error; }
    .notice-message { color: $text-muted; }
    #composer-shell {
        height: 5;
        padding: 0 2;
        border-top: solid $panel-lighten-2;
        background: $surface;
    }
    #approval-bar {
        display: none;
        height: 4;
        padding: 0;
        border: none;
        background: $surface;
    }
    #approval-prompt { height: 1; color: $text; }
    #approval-actions {
        height: 3;
        align: left middle;
    }
    #approval-actions Button {
        width: 14;
        min-width: 12;
        height: 3;
        margin-right: 1;
        color: $text;
        background: $surface;
        border: round $panel-lighten-2;
        text-style: none;
    }
    #approval-actions Button:hover {
        color: $text;
        background: $boost;
        border: heavy $panel-lighten-3;
    }
    #approval-actions Button:focus {
        color: $text;
        background: $boost;
        border: double $panel-lighten-3;
        text-style: bold reverse;
    }
    #composer {
        height: 3;
        border: round $panel-lighten-2;
        padding: 0 1;
    }
    #key-hints {
        height: 1;
        width: 1fr;
        color: $text-muted;
        padding-left: 1;
    }
    #composer-footer {
        height: 1;
        width: 100%;
    }
    #cache-status {
        height: 1;
        width: 14;
        min-width: 14;
        padding-right: 1;
        color: $text-muted;
        text-align: right;
    }
    #command-menu {
        display: none;
        layer: overlay;
        dock: bottom;
        width: 76%;
        max-width: 88;
        height: auto;
        max-height: 14;
        margin: 0 2 5 2;
        border: round $panel-lighten-2;
        background: $surface;
        scrollbar-size-vertical: 1;
    }
    #model-menu {
        display: none;
        layer: overlay;
        dock: top;
        width: 52;
        max-width: 70%;
        height: auto;
        max-height: 14;
        margin: 3 14 0 24;
        border: round $panel-lighten-2;
        background: $surface;
        scrollbar-size-vertical: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt_or_exit", "取消", priority=True),
        Binding("ctrl+d", "exit_chat", "退出", priority=True),
        Binding("ctrl+l", "focus_composer", "输入", show=False),
        Binding("pageup", "history_up", "历史", show=False),
        Binding("pagedown", "history_down", "最新", show=False),
        Binding("shift+tab", "cycle_model", "切换模型", show=False, priority=True),
    ]

    def __init__(
        self,
        client: GatewayClient,
        project_id: str,
        *,
        session_id: str | None,
        records: list[dict[str, Any]],
        external_command: ExternalCommand,
        history_displayed: HistoryDisplayed | None = None,
        unread_count: int = 0,
        initial_observer_status: dict[str, Any] | None = None,
        model_name: str | None = None,
        model_options: tuple[Any, ...] = (),
        initial_notices: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.client = client
        self.project_id = project_id
        self.session_id = session_id
        self.records = records
        self.external_command = external_command
        self.history_displayed = history_displayed
        self.unread_count = unread_count
        self.initial_observer_status = initial_observer_status
        self.initial_notices = initial_notices
        self.current_run_id: str | None = None
        self._active_worker: Worker[Any] | None = None
        self._assistant: Markdown | None = None
        self._assistant_text = ""
        self._stream_dirty = False
        self._turn_container: Vertical | None = None
        self._turn_trace: TurnTraceDisclosure | None = None
        self._turn_started_at: float | None = None
        self._activity_cards: dict[int, LoopActivityCard] = {}
        self._current_activity: LoopActivityCard | None = None
        self._activity_epoch = 0
        self._command_values: list[str] = []
        self._suppress_command_menu_value: str | None = None
        self._terminal_status: str | None = None
        self._last_ctrl_c = 0.0
        self._current_status = "就绪"
        self._approval_future: asyncio.Future[bool] | None = None
        self._code_mode = False
        self._code_session: Any | None = None
        self._code_event_sequence = 0
        self._code_progress: list[str] = []
        self._saved_observer: tuple[str, str] | None = None
        self._data_mode: str | None = None
        self._observer_caption_text = (
            "已恢复最近 Turn · 仅可见事件"
            if self.initial_observer_status else "当前 Turn · 仅可见事件"
        )
        self._observer_progress_text = str(
            (self.initial_observer_status or {}).get("progress_markdown")
            or "等待下一轮任务"
        )
        self._cache_metrics: list[dict[str, Any]] = []
        self._initial_cache_status = _restored_cache_status(records)
        self._main_cache_status = self._initial_cache_status
        self._code_cache_metrics: list[dict[str, Any]] = []
        self._code_cache_status = "缓存 ??.?%"
        self._model_name = model_name or _restored_model_name(records)
        self._model_options = [
            {
                "profile_id": str(getattr(item, "profile_id", "default")),
                "provider": str(getattr(item, "provider", "")),
                "model": str(getattr(item, "model", "")),
                "selected": bool(getattr(item, "selected", False)),
                "reasoning_effort": str(
                    getattr(item, "reasoning_effort", "low") or "low"
                ),
            }
            for item in model_options
        ]
        if not self._model_options:
            self._model_options = [{
                "profile_id": "default", "provider": "",
                "model": self._model_name or "model", "selected": True,
                "reasoning_effort": "low",
            }]
        selected_model = next(
            (item for item in self._model_options if item["selected"]),
            self._model_options[0],
        )
        self._model_profile_id = str(selected_model["profile_id"])
        self._model_name = str(selected_model["model"] or self._model_name or "model")
        configured_effort = str(selected_model["reasoning_effort"])
        self._reasoning_effort = (
            configured_effort if configured_effort in REASONING_EFFORTS else "low"
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("YUAN YE", id="brand")
            yield Static("LOCAL GATEWAY  /  新会话", id="session-context")
            yield Button(self._model_name, id="model-switch")
            yield Select(
                [(effort, effort) for effort in REASONING_EFFORTS],
                value=self._reasoning_effort,
                allow_blank=False,
                compact=True,
                id="reasoning-switch",
            )
            yield Static("就绪", id="run-status")
        with Horizontal(id="workspace"):
            yield ConversationTimeline(id="timeline")
            yield ConversationTimeline(id="code-timeline")
            with VerticalScroll(id="data-view"):
                yield Static("", id="data-content", markup=False)
            with VerticalScroll(id="observer-pane"):
                yield Static("TURN OBSERVER", id="observer-heading")
                yield Static(self._observer_caption_text, id="observer-caption")
                yield Markdown(
                    self._observer_progress_text,
                    id="observer-progress",
                )
        yield OptionList(id="command-menu", compact=True)
        yield OptionList(id="model-menu", compact=True)
        with Vertical(id="composer-shell"):
            with Vertical(id="approval-bar"):
                yield Static("", id="approval-prompt")
                with Horizontal(id="approval-actions"):
                    yield Button("允许", id="approval-allow", variant="default")
                    yield Button("拒绝", id="approval-deny", variant="default")
            yield Input(
                placeholder=_COMPOSER_PLACEHOLDER,
                id="composer",
            )
            with Horizontal(id="composer-footer"):
                yield Static(
                "Enter 发送  ·  / 命令  ·  ↑↓ 选择  ·  Tab 补全  ·  "
                "Ctrl+C 中断 / 再按退出  ·  滚轮查看历史",
                    id="key-hints",
                )
                yield Static(self._initial_cache_status, id="cache-status")

    async def on_mount(self) -> None:
        await self._restore_history()
        for notice in self.initial_notices:
            await self._append_notice(notice)
        if self.history_displayed is not None:
            # A read receipt follows the actual presentation boundary.  A
            # transient projection failure must not prevent chat startup.
            try:
                warning = await self.history_displayed()
            except Exception:
                warning = None
            if warning:
                await self._append_notice(warning)
        self._set_status("就绪")
        # Coalesce token events. Re-parsing Markdown for every tiny provider
        # chunk makes the terminal appear slower even though Gateway streaming
        # itself is healthy. The event consumer remains unblocked while this
        # presentation timer paints at most 30 frames per second.
        self.set_interval(1 / 30, self._flush_stream)
        self._apply_responsive_layout(self.size.width)
        # Markdown descendants complete their layout after ``on_mount``. Pin
        # once more on the next settled frame so restored sessions open at the
        # true bottom rather than at the pre-layout zero offset.
        self.set_timer(
            0.05,
            self.query_one("#timeline", ConversationTimeline).pin_to_tail,
        )
        self.query_one("#composer", Input).focus()

    def on_resize(self, event: Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    def on_click(self, event: Click) -> None:
        menu = self.query_one("#model-menu", OptionList)
        if not menu.display:
            return
        node = getattr(event, "widget", None)
        while node is not None:
            if getattr(node, "id", None) in {"model-menu", "model-switch"}:
                return
            node = getattr(node, "parent", None)
        self._hide_model_menu()

    def _apply_responsive_layout(self, width: int) -> None:
        observer = self.query_one("#observer-pane", VerticalScroll)
        observer.display = self._data_mode is None and width >= 92

    @on(Input.Changed, "#composer")
    def update_command_menu(self, event: Input.Changed) -> None:
        self._hide_model_menu()
        value = event.value.lstrip()
        if event.value == self._suppress_command_menu_value:
            self._suppress_command_menu_value = None
            self._hide_command_menu()
            return
        menu = self.query_one("#command-menu", OptionList)
        if not value.startswith("/") or "\n" in value:
            self._hide_command_menu()
            return
        query = value.casefold()
        if self._code_mode:
            commands = (
                ("/exit", "验证并合并后返回 Main Agent"),
                ("/abort", "放弃 Code Session 后返回 Main Agent"),
            )
        elif self._data_mode == "inbox":
            commands = (
                ("/inbox", "刷新未读结果"),
                ("/inbox all", "显示全部结果"),
                ("/inbox show <ID>", "查看完整结果"),
                ("/inbox read <ID>", "标记一条已读"),
                ("/inbox read-all", "全部标记已读"),
                ("/exit", "返回 Main Agent"),
            )
        elif self._data_mode == "skill":
            commands = (
                ("/skill list", "刷新 Skill 列表"),
                ("/skill audit <review-id>", "查看审核结果"),
                ("/skill refresh", "刷新当前 Session Skill"),
                ("/exit", "返回 Main Agent"),
            )
        else:
            commands = COMMANDS
        matches = [
            (command, description)
            for command, description in commands
            if command.casefold().startswith(query)
            or query in command.casefold()
            or query[1:] in description.casefold()
        ]
        menu.clear_options()
        self._command_values = [command for command, _ in matches]
        menu.add_options([
            Option(Text.assemble(
                (command, "bold cyan"), (f"  {description}", "dim"),
            ), id=str(index))
            for index, (command, description) in enumerate(matches)
        ])
        menu.display = bool(matches)
        if matches:
            menu.highlighted = 0

    @on(OptionList.OptionSelected, "#command-menu")
    def choose_command(self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        if 0 <= index < len(self._command_values):
            self._fill_command(self._command_values[index])

    @on(Button.Pressed, "#model-switch")
    def toggle_model_menu(self, event: Button.Pressed) -> None:
        event.stop()
        if self._worker_running:
            return
        menu = self.query_one("#model-menu", OptionList)
        if menu.display:
            self._hide_model_menu()
            self.query_one("#composer", Input).focus()
            return
        self._hide_command_menu()
        menu.clear_options()
        menu.add_options([
            Option(
                Text.assemble(
                    (str(item["model"]), "bold"),
                    (f"  {item['provider']}", "dim"),
                ),
                id=str(item["profile_id"]),
            )
            for item in self._model_options
        ])
        current = next(
            (
                index for index, item in enumerate(self._model_options)
                if item["profile_id"] == self._model_profile_id
            ),
            0,
        )
        menu.highlighted = current
        menu.display = True
        menu.focus()

    @on(OptionList.OptionSelected, "#model-menu")
    def choose_model(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._model_options):
            self._select_model(event.option_index)
        event.stop()

    @on(Select.Changed, "#reasoning-switch")
    def choose_reasoning_effort(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str) or self._worker_running:
            return
        self._reasoning_effort = event.value

    def _select_model(self, index: int) -> None:
        selected = self._model_options[index % len(self._model_options)]
        self._model_profile_id = str(selected["profile_id"])
        self._model_name = str(selected["model"])
        self.query_one("#model-switch", Button).label = self._model_name
        self._hide_model_menu()
        self.query_one("#composer", Input).focus()

    def _hide_model_menu(self) -> None:
        self.query_one("#model-menu", OptionList).display = False

    def on_key(self, event: Key) -> None:
        if self._approval_future is not None and not self._approval_future.done():
            if event.key == "left":
                self.query_one("#approval-allow", Button).focus()
            elif event.key == "right":
                self.query_one("#approval-deny", Button).focus()
            elif event.key == "escape":
                self._resolve_inline_approval(False)
            else:
                return
            event.prevent_default()
            event.stop()
            return
        model_menu = self.query_one("#model-menu", OptionList)
        if model_menu.display:
            if event.key == "escape":
                self._hide_model_menu()
                self.query_one("#composer", Input).focus()
                event.prevent_default()
                event.stop()
            # Arrow/Enter navigation belongs to the focused OptionList. Any
            # other operation closes the transient card first.
            elif event.key not in {"up", "down", "enter"}:
                self._hide_model_menu()
        composer = self.query_one("#composer", Input)
        menu = self.query_one("#command-menu", OptionList)
        if not composer.has_focus or not menu.display or not self._command_values:
            return
        if event.key in {"down", "up"}:
            current = int(menu.highlighted or 0)
            direction = 1 if event.key == "down" else -1
            menu.highlighted = (current + direction) % len(self._command_values)
            menu.scroll_to_highlight()
            event.prevent_default()
            event.stop()
        elif event.key == "tab":
            index = int(menu.highlighted or 0)
            self._fill_command(self._command_values[index])
            event.prevent_default()
            event.stop()

    def _fill_command(self, value: str) -> None:
        # Placeholders describe required input but are not inserted literally.
        completed = value.split(" <", 1)[0]
        composer = self.query_one("#composer", Input)
        filled = completed + (" " if " <" in value else "")
        self._suppress_command_menu_value = filled
        composer.value = filled
        composer.cursor_position = len(composer.value)
        self._hide_command_menu()
        composer.focus()

    def _hide_command_menu(self) -> None:
        menu = self.query_one("#command-menu", OptionList)
        menu.display = False
        self._command_values = []

    @on(Button.Pressed, "#approval-allow")
    def approve_tool_request(self, event: Button.Pressed) -> None:
        event.stop()
        self._resolve_inline_approval(True)

    @on(Button.Pressed, "#approval-deny")
    def deny_tool_request(self, event: Button.Pressed) -> None:
        event.stop()
        self._resolve_inline_approval(False)

    @on(Input.Submitted, "#composer")
    async def submit(self, event: Input.Submitted) -> None:
        self._hide_model_menu()
        value = event.value.strip()
        if not value or self._worker_running:
            return
        menu = self.query_one("#command-menu", OptionList)
        if menu.display and self._command_values:
            selected = self._command_values[int(menu.highlighted or 0)]
            normalized = selected.split(" <", 1)[0]
            if value != normalized:
                self._fill_command(selected)
                return
        self._hide_command_menu()
        event.input.value = ""
        if self._data_mode is not None:
            if value in {"/exit", "/quit"}:
                self._leave_data_mode()
                return
            expected = f"/{self._data_mode}"
            if not value.startswith(expected):
                self._update_data_view(
                    f"当前是 {expected} 数据视图；请输入 {expected} 子命令，"
                    "或使用 /exit 返回 Main Agent。",
                )
                return
            self._active_worker = self.run_external_command(value)
            return
        if self._code_mode:
            self.query_one("#code-timeline", ConversationTimeline).return_to_live_edge()
            if value in {"/exit", "/quit"}:
                self._active_worker = self.finalize_code_mode()
            elif value == "/abort":
                self._active_worker = self.abort_code_mode()
            elif value == "/help":
                await self._append_code_notice(
                    "/exit 验证并合并 · /abort 放弃全部修改 · "
                    "连续两次 Ctrl+C 保留现场并返回 Main Agent",
                )
            else:
                await self._append_code_user(value)
                self._active_worker = self.run_code_turn(value)
            return
        # Sending a new prompt is an explicit return to the live edge. History
        # scrolling remains sticky only while reading; a new Turn must start
        # with the user's just-submitted message visible at the bottom.
        self.query_one("#timeline", ConversationTimeline).return_to_live_edge()
        if value in {"/exit", "/quit"}:
            self.exit(self.session_id)
            return
        if value == "/help":
            await self._append_notice(
                "/code · /compress · /context refresh · /skill · /inbox · "
                "/tool-result · /extension · /reload · /cron · /dream · /harness · /exit"
            )
            return
        if self._is_local_command(value):
            data_mode = self._data_view_kind(value)
            if data_mode is not None:
                self._enter_data_mode(data_mode)
            self._active_worker = self.run_external_command(value)
        else:
            await self._append_user(value)
            self.query_one("#timeline", ConversationTimeline).return_to_live_edge()
            self._active_worker = self.run_agent(value)

    @work(exclusive=True, group="gateway-command", exit_on_error=False)
    async def run_external_command(self, command: str) -> None:
        data_mode = self._data_view_kind(command)
        if data_mode is not None and self._data_mode is None:
            self._enter_data_mode(data_mode)
        self._set_busy(True, f"执行 {command.split(maxsplit=1)[0]}")
        try:
            if command == "/code":
                await self._enter_code_mode()
            else:
                output = await self.external_command(command, self.session_id)
                message = output or f"命令完成：{command.split(maxsplit=1)[0]}"
                if self._data_mode is not None:
                    self._update_data_view(message)
                else:
                    await self._append_notice(message)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if self._data_mode is not None:
                self._update_data_view(message)
            else:
                await self._append_error(message)
        finally:
            self._set_busy(
                False,
                "Code 就绪" if self._code_mode
                else "数据视图" if self._data_mode is not None
                else "就绪",
            )

    def command_output_width(self) -> int:
        """Return the usable width for Rich output mounted in the timeline."""
        if self._data_mode is not None:
            workspace = self.query_one("#workspace", Horizontal)
            return max(48, workspace.content_size.width - 8)
        timeline = self.query_one("#timeline", ConversationTimeline)
        # Account for the notice widget's horizontal margin and padding so
        # Rich output is laid out once rather than re-wrapped by Textual.
        return max(32, timeline.content_size.width - 8)

    @staticmethod
    def _data_view_kind(command: str) -> str | None:
        head = command.split(maxsplit=1)[0]
        return head[1:] if head in {"/inbox", "/skill"} else None

    def _enter_data_mode(self, kind: str) -> None:
        self._data_mode = kind
        self.query_one("#timeline", ConversationTimeline).display = False
        self.query_one("#code-timeline", ConversationTimeline).display = False
        self.query_one("#observer-pane", VerticalScroll).display = False
        view = self.query_one("#data-view", VerticalScroll)
        view.display = True
        self.query_one("#data-content", Static).update("正在加载…")
        self.query_one("#brand", Static).update(kind.upper())
        self.query_one("#session-context", Static).update("DATA VIEW  /  FULL WIDTH  /")
        composer = self.query_one("#composer", Input)
        composer.placeholder = f"输入 /{kind} 子命令，或 /exit 返回 Main Agent"
        self.query_one("#key-hints", Static).update(
            "Enter 执行  ·  / 命令  ·  Ctrl+C / /exit 返回 Main Agent"
        )
        self._set_status("数据视图")

    def _leave_data_mode(self) -> None:
        if self._data_mode is None:
            return
        self._data_mode = None
        self.query_one("#data-view", VerticalScroll).display = False
        self.query_one("#timeline", ConversationTimeline).display = True
        self._apply_responsive_layout(self.size.width)
        self.query_one("#brand", Static).update("YUAN YE")
        self.query_one("#composer", Input).placeholder = _COMPOSER_PLACEHOLDER
        self.query_one("#key-hints", Static).update(
            "Enter 发送  ·  / 命令  ·  ↑↓ 选择  ·  Tab 补全  ·  "
            "Ctrl+C 中断 / 再按退出  ·  滚轮查看历史"
        )
        self._set_status("就绪")
        self._update_brand()
        self.query_one("#timeline", ConversationTimeline).return_to_live_edge()

    def _update_data_view(self, content: str) -> None:
        self.query_one("#data-content", Static).update(Text(
            content,
            no_wrap=True,
            overflow="crop",
        ))
        view = self.query_one("#data-view", VerticalScroll)
        self.call_after_refresh(view.scroll_home)

    async def confirm(self, title: str, message: str) -> bool:
        """Request a command confirmation without leaving the TUI."""
        return await self._request_inline_confirmation(f"{title} · {message}")

    async def _enter_code_mode(self) -> None:
        if self._code_session is None:
            self._set_status("启动 Code")
            self._code_session = await self.client.start_code_session(
                self.project_id, self.session_id,
            )
            self._code_event_sequence = 0
            self._code_progress.clear()
            self._code_cache_metrics = []
            self._code_cache_status = "缓存 ??.?%"
            await self._append_code_notice(
                f"隔离 Coding Session 已就绪 · 分支 {self._code_session.branch}",
            )
        self._saved_observer = (
            self._observer_caption_text, self._observer_progress_text,
        )
        self._code_mode = True
        self.query_one("#timeline", ConversationTimeline).display = False
        code_timeline = self.query_one("#code-timeline", ConversationTimeline)
        code_timeline.display = True
        code_timeline.return_to_live_edge()
        self.query_one("#brand", Static).update("YY CODE")
        self.query_one("#observer-heading", Static).update("CODE OBSERVER")
        self._update_observer(
            "当前 Coding Session · 隔离 Worktree",
            "等待 Coding 需求",
        )
        self.query_one("#cache-status", Static).update(self._code_cache_status)
        self._update_brand()
        composer = self.query_one("#composer", Input)
        composer.placeholder = "输入 Coding 需求，或使用 /exit、/abort"
        self.query_one("#key-hints", Static).update(
            "Enter 执行  ·  /exit 合并  ·  /abort 放弃  ·  "
            "连续两次 Ctrl+C 返回 Main Agent",
        )

    def _leave_code_mode(self, *, preserved: bool) -> None:
        if not self._code_mode:
            return
        self._code_mode = False
        self.query_one("#code-timeline", ConversationTimeline).display = False
        timeline = self.query_one("#timeline", ConversationTimeline)
        timeline.display = True
        timeline.return_to_live_edge()
        self.query_one("#brand", Static).update("YUAN YE")
        self.query_one("#observer-heading", Static).update("TURN OBSERVER")
        if self._saved_observer is not None:
            self._update_observer(*self._saved_observer)
        self._saved_observer = None
        self.query_one("#cache-status", Static).update(self._main_cache_status)
        self.query_one("#composer", Input).placeholder = _COMPOSER_PLACEHOLDER
        self.query_one("#key-hints", Static).update(
            "Enter 发送  ·  / 命令  ·  ↑↓ 选择  ·  Tab 补全  ·  "
            "Ctrl+C 中断 / 再按退出  ·  滚轮查看历史",
        )
        self._update_brand()
        self._set_composer_notice(
            "Coding Session 已保留，可再次输入 /code 继续"
            if preserved else "已返回 Main Agent",
        )

    @work(exclusive=True, group="gateway-command", exit_on_error=False)
    async def run_code_turn(self, task: str) -> None:
        self._set_busy(True, "Code 运行中")
        pending = asyncio.create_task(
            self.client.run_code_turn(
                self._code_session.code_session_id,
                task,
                model_profile_id=self._model_profile_id,
                reasoning_effort=self._reasoning_effort,
            ),
        )
        try:
            while not pending.done():
                await self._poll_code_events()
                await asyncio.sleep(0.25)
            result = await pending
            await self._poll_code_events()
            for metric in result.model_calls:
                if isinstance(metric, dict):
                    self._code_cache_metrics.append(metric)
            if any(_cache_values(metric) is not None for metric in result.model_calls):
                self._code_cache_status = _cache_status_text(self._code_cache_metrics)
                self.query_one("#cache-status", Static).update(self._code_cache_status)
            details = [
                result.message,
                f"测试文件：{result.test_file}",
                f"尝试次数：{result.attempts}",
            ]
            if result.commit:
                details.append(f"临时提交：{result.commit}")
            if result.diagnostic:
                details.append(f"诊断：\n{result.diagnostic}")
            await self._append_code_assistant("\n\n".join(details))
            self._update_observer(
                "本轮 Coding 已结束",
                "✓ 验证通过" if result.status == "verified" else "⚠ 验证未通过",
            )
        except asyncio.CancelledError:
            # The Gateway owns the durable Coding operation. Do not pretend a
            # cancelled local waiter cancelled work which may still be running.
            pending.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None,
            )
            await self._append_code_notice("客户端停止等待；Coding 操作仍由 Gateway 持续记录")
            raise
        except Exception as exc:
            await self._append_code_error(str(exc) or type(exc).__name__)
            self._update_observer("Coding Turn 失败", f"⚠ {str(exc) or type(exc).__name__}")
        finally:
            self._set_busy(False, "Code 就绪" if self._code_mode else "就绪")

    async def _poll_code_events(self) -> None:
        if self._code_session is None:
            return
        events = await self.client.code_session_events(
            self._code_session.code_session_id,
            after_sequence=self._code_event_sequence,
        )
        labels = {
            "code_turn_started": "正在分析需求并分配测试文件",
            "code_generation": "Coding Agent 正在生成代码",
            "code_auto_repair": "测试未通过，正在自动修复",
            "code_test": "正在执行验证命令",
            "code_turn_verified": "验证通过并已创建临时提交",
            "code_turn_unverified": "自动修复后仍未通过验证",
        }
        for event in events:
            self._code_event_sequence = max(
                self._code_event_sequence, int(event.get("sequence", 0)),
            )
            label = labels.get(str(event.get("record_type", "")))
            if not label:
                continue
            if str(event.get("record_type")) == "code_test":
                command = event.get("command")
                if isinstance(command, list):
                    label += "：" + " ".join(str(item) for item in command)
            self._code_progress.append(label)
            await self._append_code_notice(f"◌ {label}")
            self._update_observer(
                "当前 Coding Turn · Harness 可见事件",
                "## 进行中\n\n" + label + "\n\n## 已完成\n\n" + "\n".join(
                    f"- {item}" for item in self._code_progress[:-1]
                ),
            )

    @work(exclusive=True, group="gateway-command", exit_on_error=False)
    async def finalize_code_mode(self) -> None:
        self._set_busy(True, "验证并合并")
        try:
            result = await self.client.finalize_code_session(
                self._code_session.code_session_id,
            )
            if result.status == "capability_confirmation_required":
                plan = result.grant_plan
                lines = []
                for hook in plan.get("hooks", []):
                    capabilities = hook.get("confirmation_required_capabilities", [])
                    tools = [item.get("name") for item in hook.get("tools", [])]
                    if capabilities or tools:
                        lines.append(
                            f"{hook.get('hook_id')}: capabilities={capabilities or '-'}; "
                            f"tools={tools or '-'}"
                        )
                approved = await self.push_screen_wait(ConfirmModal(
                    "Extension 权限确认",
                    "\n".join(lines) or "没有新增受控权限",
                ))
                if not approved:
                    await self._append_code_notice("已取消合并，Coding Session 保持活动")
                    return
                result = await self.client.finalize_code_session(
                    self._code_session.code_session_id,
                    str(plan["plan_hash"]),
                )
            await self._append_code_notice(result.message)
            if not result.stay_in_code_mode:
                self._code_session = None
                self._leave_code_mode(preserved=False)
                await self._append_notice(result.message)
        except Exception as exc:
            await self._append_code_error(str(exc) or type(exc).__name__)
        finally:
            self._set_busy(False, "Code 就绪" if self._code_mode else "就绪")

    @work(exclusive=True, group="gateway-command", exit_on_error=False)
    async def abort_code_mode(self) -> None:
        approved = await self.confirm(
            "放弃 Coding Session",
            "将删除当前隔离修改，且不能恢复。是否继续？",
        )
        if not approved:
            await self._append_code_notice("已取消放弃操作")
            return
        self._set_busy(True, "放弃 Code")
        try:
            result = await self.client.abort_code_session(
                self._code_session.code_session_id,
            )
            self._code_session = None
            self._leave_code_mode(preserved=False)
            await self._append_notice(result.message)
        except Exception as exc:
            await self._append_code_error(str(exc) or type(exc).__name__)
        finally:
            self._set_busy(False, "就绪")

    @work(exclusive=True, group="gateway-command", exit_on_error=False)
    async def run_agent(self, prompt: str) -> None:
        self._set_busy(True, "连接 Gateway")
        self._last_ctrl_c = 0.0
        self._terminal_status = None
        self._cache_metrics = []
        self._update_observer("当前 Turn · 仅可见事件", "正在建立本轮监控")
        self._assistant_text = ""
        self._stream_dirty = False
        self._assistant = None
        self._activity_cards = {}
        self._activity_epoch = 0
        self._turn_started_at = monotonic()
        self._turn_container = Vertical(classes="turn-execution")
        await self._mount_timeline(self._turn_container)
        await self._turn_container.mount(Static(
            "YUAN YE", classes="sender assistant-sender",
        ))
        self._turn_trace = TurnTraceDisclosure()
        await self._turn_container.mount(self._turn_trace)
        self._current_activity = LoopActivityCard()
        await self._turn_trace.detail.mount(self._current_activity)
        timeline = self.query_one("#timeline", ConversationTimeline)
        timeline.return_to_live_edge()
        # Present the submitted question and compact activity row before the
        # provider call can block on network I/O.
        await timeline.wait_for_refresh()
        timeline.pin_to_tail()
        try:
            run = await self.client.start_run(
                self.project_id, prompt, self.session_id,
                model_profile_id=self._model_profile_id,
                reasoning_effort=self._reasoning_effort,
            )
            if getattr(run, "session_id", None):
                self.session_id = str(run.session_id)
                self._update_brand()
            self.current_run_id = run.run_id
            self._set_status("运行中")
            async with contextlib.aclosing(
                self.client.subscribe(run.run_id),
            ) as subscription:
                async for event in subscription:
                    if event.session_id:
                        self.session_id = event.session_id
                        self._update_brand()
                    await self._consume_gateway_event(event)
            await self._finish_observer(run.run_id)
        except asyncio.CancelledError:
            if self.current_run_id:
                with contextlib.suppress(Exception):
                    await self.client.cancel_run(self.current_run_id)
            await self._append_turn_notice("已请求取消当前回答")
            self._terminal_status = "已取消"
        except Exception as exc:
            await self._append_turn_error(str(exc) or type(exc).__name__)
            self._terminal_status = "执行失败"
        finally:
            self.current_run_id = None
            self._assistant = None
            self._set_busy(False, self._terminal_status or "就绪")
            timeline = self.query_one("#timeline", ConversationTimeline)
            if timeline.follow_tail:
                await timeline.wait_for_refresh()
                timeline.follow_tail_if_enabled()

    async def _consume_gateway_event(self, event: Any) -> None:
        raw_type = event.type
        event_type = raw_type.value if isinstance(raw_type, EventType) else str(raw_type)
        payload = dict(event.payload)
        if event_type == EventType.TEXT.value:
            if (
                self._current_activity is not None
                and self._current_activity.tools
                and not any(
                    item.get("status") == "running"
                    for item in self._current_activity.tools
                )
            ):
                self._current_activity.show_thinking()
            if self._assistant is None:
                self._assistant = await self._append_assistant_segment("")
                self._move_activity_to_turn_tail()
            self._assistant_text += str(payload.get("content", ""))
            self._stream_dirty = True
            return
        if event_type == EventType.REASONING.value:
            loop = int(payload.get("loop") or 0)
            card = self._activity_cards.get(loop)
            if card is None:
                if (
                    self._current_activity is not None
                    and not self._current_activity.tools
                    and not self._current_activity.loop
                ):
                    card = self._current_activity
                    card.loop = loop
                else:
                    if self._current_activity is not None:
                        self._current_activity.finish_loop()
                    card = LoopActivityCard(loop)
                    await self._mount_turn_widget(card)
                self._activity_cards[loop] = card
            self._current_activity = card
            card.append_reasoning(payload.get("content"))
            return
        if event_type == EventType.TOOL_REQUESTED.value:
            self._flush_stream()
            self._assistant = None
            self._assistant_text = ""
            loop = int(payload.get("loop") or 0)
            self._activity_epoch += 1
            card = self._activity_cards.get(loop)
            if card is None:
                if self._current_activity is not None:
                    if not self._current_activity.tools and not self._current_activity.loop:
                        card = self._current_activity
                        card.loop = loop
                    else:
                        self._current_activity.finish_loop()
                        card = LoopActivityCard(loop)
                        await self._mount_turn_widget(card)
                else:
                    card = LoopActivityCard(loop)
                    await self._mount_turn_widget(card)
                self._activity_cards[loop] = card
            self._current_activity = card
            card.start_tool(payload)
            self._move_activity_to_turn_tail(card)
            self._follow_timeline()
        elif event_type == EventType.TOOL_COMPLETED.value:
            loop = int(payload.get("loop") or 0)
            card = self._activity_cards.get(loop)
            if card is None:
                card = LoopActivityCard(loop)
                card.start_tool(payload)
                self._activity_cards[loop] = card
                self._current_activity = card
                await self._mount_turn_widget(card)
            card.complete_tool(payload)
            self._activity_epoch += 1
            epoch = self._activity_epoch
            self.set_timer(
                0.06,
                lambda: self._resume_thinking_after_tools(card, epoch),
            )
            self._follow_timeline()
        elif event_type == EventType.TOOL_BATCH_COMPLETED.value:
            # Individual observations normally complete every row first.  The
            # batch boundary is also a deterministic UI fallback after replay
            # or reconnect, so an activity card can never remain "executing"
            # after Core has declared the parallel group settled.
            card = self._current_activity
            if card is not None:
                card.settle_pending_tools()
            self._follow_timeline()
        elif event_type == EventType.MODEL_RETRY.value:
            await self._append_turn_notice(
                f"模型连接异常，{payload.get('delay_seconds', 2)} 秒后重试",
            )
        elif event_type == EventType.MODEL_RECONNECTED.value:
            await self._append_turn_notice("模型连接已恢复")
        elif event_type == EventType.MODEL_USAGE.value:
            model = payload.get("model")
            if isinstance(model, dict) and model.get("name"):
                self._model_name = str(model["name"])
                self._update_brand()
            metric = payload.get("model_call")
            if isinstance(metric, dict):
                self._cache_metrics.append(metric)
                # An unavailable usage block is not a new percentage. Keep
                # the last known value instead of making the footer flicker
                # from a real ratio back to an indeterminate placeholder.
                if _cache_values(metric) is not None:
                    self._main_cache_status = _cache_status_text(self._cache_metrics)
                    self.query_one("#cache-status", Static).update(self._main_cache_status)
        elif event_type == EventType.FINAL.value:
            if self._current_activity is not None:
                self._current_activity.set_reasoning_summary(payload.get("reasoning"))
        elif event_type == EventType.SANDBOX_FALLBACK.value:
            await self._append_turn_notice(str(
                payload.get("message")
                or "操作系统沙箱不可用，已进入 checkpoint-only 模式",
            ))
        elif event_type == EventType.COMPRESSION_STARTED.value:
            await self._append_turn_notice("◌ 正在压缩上下文")
        elif event_type == EventType.CONTEXT_COMPRESSED.value:
            await self._append_turn_notice("✓ 上下文压缩完成")
        elif event_type == "observer_progress":
            # A delayed non-terminal projection must not roll a completed Turn
            # back to an active-looking sidebar.
            if self._terminal_status is None:
                self._update_observer(
                    progress=str(payload.get("progress_markdown") or "正在观察"),
                )
        elif event_type == "approval_requested":
            approved = await self._request_inline_approval(payload)
            await self.client.respond_approval(str(payload["approval_id"]), approved)
        elif event_type == "harness_evolution_proposed":
            approved = await self.push_screen_wait(ConfirmModal(
                "Harness 修复建议",
                "检测到可能可修复的源码缺陷，是否启动隔离 Harness？",
            ))
            result = await self.client.decide_harness_evolution(
                str(payload["proposal_id"]), approved,
            )
            await self._append_turn_notice(
                f"Harness：{(result.get('result') or {}).get('status') or result.get('status')}",
            )
        elif event_type == "run_completed":
            answer = str(payload.get("answer") or self._assistant_text)
            await self._replace_turn_with_final(answer)
            # Observer reduction is isolated from the Main Run and may finish
            # after its canonical terminal event. Never leave the stale active
            # projection beside an already completed Run.
            self._update_observer(
                "本轮已完成 · 最终状态核对",
                "✓ 任务已完成\n\n正在完成最终意图核对…",
            )
            self._terminal_status = "已完成"
            self._set_status("已完成")
            with contextlib.suppress(Exception):
                await self.client.acknowledge_run_result(event.run_id)
        elif event_type in {"run_failed", "run_cancelled", "run_interrupted"}:
            message = str(payload.get("message") or event_type)
            await self._append_turn_error(message)
            self._update_observer(
                "本轮已结束 · 最终状态核对",
                "⚠ 主任务已结束\n\n正在完成最终状态核对…",
            )
            self._terminal_status = (
                "已取消" if event_type == "run_cancelled" else "执行失败"
            )
            self._set_status(self._terminal_status)

    def _update_observer(
        self,
        caption: str | None = None,
        progress: str | None = None,
    ) -> None:
        if caption is not None:
            self._observer_caption_text = caption
            self.query_one("#observer-caption", Static).update(caption)
        if progress is not None:
            self._observer_progress_text = progress
            self.query_one("#observer-progress", Markdown).update(progress)

    async def _finish_observer(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        status: dict[str, Any] | None = None
        while True:
            try:
                status = await self.client.observer_status(run_id)
                if status.get("status") in {"finalized", "failed"}:
                    break
            except Exception:
                pass
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.1)
        # An active snapshot still describes work from before the terminal Run
        # event. It must not overwrite the terminal placeholder with a stale
        # "进行中" list merely because the isolated Observer exceeded grace.
        if not status or status.get("status") not in {"finalized", "failed"}:
            return
        self._update_observer(
            progress=str(status.get("progress_markdown") or "✓ 任务已完成"),
        )
        proposal = status.get("correction_proposal")
        if isinstance(proposal, dict) and proposal.get("status") == "pending":
            action, edited_prompt = await self.push_screen_wait(CorrectionModal(
                str(proposal.get("reason") or ""),
                str(proposal.get("proposed_prompt") or ""),
            ))
            await self.client.decide_observer_correction(
                str(proposal["proposal_id"]),
                expected_revision=int(proposal.get("revision", 0)),
                action=action,
                edited_prompt=edited_prompt,
                reason=f"tui_user_{action}",
            )

    async def _request_inline_approval(self, payload: dict[str, Any]) -> bool:
        tool_name = str(payload.get("tool_name") or "tool")
        arguments = payload.get("arguments")
        argument_text = ""
        if isinstance(arguments, dict) and arguments:
            if tool_name == "bash" and arguments.get("command"):
                argument_text = str(arguments["command"])
            else:
                argument_text = json.dumps(
                    arguments, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
            argument_text = argument_text.replace("\n", " ")[:100]
        prompt = f"工具 {tool_name} 请求执行 · ← → 选择，Enter 确定"
        if argument_text:
            prompt += f"  {argument_text}"
        return await self._request_inline_confirmation(prompt)

    async def _request_inline_confirmation(self, prompt: str) -> bool:
        """Temporarily replace the composer with one compact approval control."""

        if self._approval_future is not None and not self._approval_future.done():
            raise RuntimeError("已有确认正在等待处理")
        future = asyncio.get_running_loop().create_future()
        self._approval_future = future

        bar = self.query_one("#approval-bar", Vertical)
        composer = self.query_one("#composer", Input)
        footer = self.query_one("#composer-footer", Horizontal)
        self.query_one("#approval-prompt", Static).update(prompt)
        self._hide_command_menu()
        composer.display = False
        footer.display = False
        bar.display = True
        allow = self.query_one("#approval-allow", Button)
        self.call_after_refresh(allow.focus)
        try:
            return await future
        finally:
            bar.display = False
            composer.display = True
            footer.display = True
            self._approval_future = None
            self.call_after_refresh(composer.focus)

    def _resolve_inline_approval(self, approved: bool) -> None:
        future = self._approval_future
        if future is not None and not future.done():
            future.set_result(approved)

    async def _restore_history(self) -> None:
        # Completed turns retain the same collapsed process disclosure as the
        # live UI.  Canonical Session records rebuild Tool cards without ever
        # re-executing the Tool body.
        for turn in self._historical_turns():
            user = next((item for item in turn if item.get("role") == "user"), None)
            if user is not None:
                await self._append_user(str(user.get("content") or ""))
            final_index = self._historical_final_record_index(turn)
            if final_index is not None:
                await self._append_completed_historical_turn(turn, final_index)
                continue
            await self._restore_incomplete_turn(turn, include_user=False)
        self._follow_timeline()

    def _historical_turns(self) -> list[list[dict[str, Any]]]:
        turns: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for record in self.records:
            role = str(record.get("role") or "")
            current_has_user = any(item.get("role") == "user" for item in current)
            if current and (
                role == "user"
                or (role == "assistant" and not current_has_user)
            ):
                turns.append(current)
                current = []
            current.append(record)
        if current:
            turns.append(current)
        return turns

    @staticmethod
    def _historical_final_record_index(
        turn: list[dict[str, Any]],
    ) -> int | None:
        candidates: list[int] = []
        has_execution_trace = False
        for index, record in enumerate(turn):
            role = str(record.get("role") or "")
            calls = record.get("tool_calls")
            if role == "tool" or (isinstance(calls, list) and calls):
                has_execution_trace = True
            if role == "assistant" and record.get("content") and not calls:
                candidates.append(index)
        if candidates and (has_execution_trace or len(candidates) > 1):
            return candidates[-1]
        if len(candidates) == 1:
            return candidates[0]
        return None

    async def _append_completed_historical_turn(
        self,
        turn: list[dict[str, Any]],
        final_index: int,
    ) -> None:
        container = Vertical(classes="turn-execution")
        await self._mount_timeline(container)
        await container.mount(Static(
            "YUAN YE", classes="sender assistant-sender",
        ))
        trace = TurnTraceDisclosure()
        await container.mount(trace)
        await self._populate_historical_trace(
            trace,
            [record for index, record in enumerate(turn) if index != final_index],
        )
        trace.complete(
            self._historical_elapsed(turn, final_index),
            tool_names=[
                str(item.get("name") or "")
                for card in trace.query(LoopActivityCard)
                for item in card.tools
            ],
            has_process=bool(trace.detail.children),
        )
        await container.mount(Markdown(
            str(turn[final_index].get("content") or "…"),
            classes="assistant-message",
            open_links=False,
        ))

    async def _populate_historical_trace(
        self,
        trace: TurnTraceDisclosure,
        records: list[dict[str, Any]],
    ) -> None:
        historical_tools: dict[str, LoopActivityCard] = {}
        cards: list[LoopActivityCard] = []
        next_loop = 1
        mounted_content = False
        for record in records:
            role = str(record.get("role") or "")
            content = str(record.get("content") or "")
            if role == "assistant":
                if content:
                    await trace.detail.mount(Markdown(
                        content, classes="assistant-message", open_links=False,
                    ))
                    mounted_content = True
                calls = record.get("tool_calls")
                if isinstance(calls, list) and calls:
                    card = LoopActivityCard(next_loop)
                    next_loop += 1
                    cards.append(card)
                    for call in calls:
                        function = call.get("function") if isinstance(call, dict) else None
                        if not isinstance(function, dict) or not function.get("name"):
                            continue
                        raw_arguments = function.get("arguments") or {}
                        if isinstance(raw_arguments, str):
                            try:
                                raw_arguments = json.loads(raw_arguments)
                            except (TypeError, ValueError):
                                raw_arguments = {"raw": raw_arguments}
                        call_id = str(call.get("id") or "")
                        card.start_tool({
                            "tool_call_id": call_id,
                            "position": len(card.tools),
                            "name": str(function["name"]),
                            "arguments": raw_arguments if isinstance(raw_arguments, dict) else {},
                            "execution": "historical",
                        })
                        if call_id:
                            historical_tools[call_id] = card
                    if card.tools:
                        await trace.detail.mount(card)
                        mounted_content = True
            elif role == "tool":
                call_id = str(record.get("tool_call_id") or "")
                card = historical_tools.get(call_id)
                if card is None:
                    card = LoopActivityCard(next_loop)
                    next_loop += 1
                    cards.append(card)
                    card.start_tool({
                        "tool_call_id": call_id,
                        "name": record.get("name", "tool"),
                        "execution": "historical",
                    })
                    await trace.detail.mount(card)
                    mounted_content = True
                card.complete_tool({
                    "tool_call_id": call_id,
                    "name": record.get("name", "tool"),
                    "status": record.get("status", "unknown"),
                    "content": record.get("content", ""),
                    "observation_id": record.get("record_id"),
                })
            elif role == "summary":
                await trace.detail.mount(Static(
                    "已加载会话摘要", classes="tool-message notice-message",
                ))
                mounted_content = True
        for card in cards:
            card.settle_pending_tools()
            card.finish_loop()

        if not mounted_content:
            card = LoopActivityCard()
            card.finish_loop()
            await trace.detail.mount(card)

    @staticmethod
    def _historical_elapsed(
        turn: list[dict[str, Any]],
        final_index: int,
    ) -> float:
        user = next((item for item in turn if item.get("role") == "user"), None)
        if user is None:
            return 0.0
        try:
            started = datetime.fromisoformat(str(user.get("timestamp") or ""))
            completed = datetime.fromisoformat(
                str(turn[final_index].get("timestamp") or ""),
            )
            return max(0.0, (completed - started).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    async def _restore_incomplete_turn(
        self,
        turn: list[dict[str, Any]],
        *,
        include_user: bool,
    ) -> None:
        historical_tools: dict[str, LoopActivityCard] = {}
        next_loop = 1
        for record in turn:
            role = str(record.get("role") or "")
            content = str(record.get("content") or "")
            if role == "user" and include_user:
                await self._append_user(content)
            elif role == "assistant":
                if content:
                    await self._append_assistant(content)
                calls = record.get("tool_calls")
                if isinstance(calls, list):
                    card = LoopActivityCard(next_loop)
                    next_loop += 1
                    has_tools = False
                    for call in calls:
                        function = call.get("function") if isinstance(call, dict) else None
                        if isinstance(function, dict) and function.get("name"):
                            raw_arguments = function.get("arguments") or {}
                            if isinstance(raw_arguments, str):
                                try:
                                    parsed_arguments = json.loads(raw_arguments)
                                except (TypeError, ValueError):
                                    parsed_arguments = {"raw": raw_arguments}
                            else:
                                parsed_arguments = raw_arguments
                            payload = {
                                "tool_call_id": str(call.get("id") or ""),
                                "position": len(card.tools),
                                "name": str(function["name"]),
                                "arguments": (
                                    parsed_arguments
                                    if isinstance(parsed_arguments, dict) else {}
                                ),
                                "execution": "historical",
                            }
                            card.start_tool(payload)
                            has_tools = True
                            call_id = call.get("id") if isinstance(call, dict) else None
                            if isinstance(call_id, str):
                                historical_tools[call_id] = card
                    if has_tools:
                        await self._mount_timeline(card)
            elif role == "tool":
                call_id = str(record.get("tool_call_id") or "")
                card = historical_tools.get(call_id)
                if card is None:
                    card = LoopActivityCard(next_loop)
                    next_loop += 1
                    card.start_tool({
                        "tool_call_id": call_id,
                        "name": record.get("name", "tool"),
                        "execution": "historical",
                    })
                    await self._mount_timeline(card)
                card.complete_tool({
                    "tool_call_id": call_id,
                    "name": record.get("name", "tool"),
                    "status": record.get("status", "unknown"),
                    "content": record.get("content", ""),
                    "observation_id": record.get("record_id"),
                })
                if not any(item.get("status") == "running" for item in card.tools):
                    card.finish_loop()
            elif role == "summary":
                await self._append_notice("已加载会话摘要")

    async def _append_user(self, content: str) -> None:
        await self._mount_timeline(Static("YOU", classes="sender user-sender"))
        await self._mount_timeline(Static(Text(content), classes="user-message"))

    async def _append_code_user(self, content: str) -> None:
        await self._mount_code_timeline(Static("YOU", classes="sender user-sender"))
        await self._mount_code_timeline(Static(Text(content), classes="user-message"))

    async def _append_code_assistant(self, content: str) -> None:
        await self._mount_code_timeline(Static(
            "YY CODE", classes="sender assistant-sender",
        ))
        await self._mount_code_timeline(Markdown(
            content, classes="assistant-message", open_links=False,
        ))

    async def _append_code_notice(self, content: str) -> None:
        await self._mount_code_timeline(Static(
            content, classes="tool-message notice-message",
        ))

    async def _append_code_error(self, content: str) -> None:
        await self._mount_code_timeline(Static(
            content, classes="tool-message error-message",
        ))

    async def _mount_code_timeline(self, widget: Static | Markdown) -> None:
        timeline = self.query_one("#code-timeline", ConversationTimeline)
        should_follow = timeline.follow_tail
        await timeline.mount(widget)
        if should_follow:
            self.call_after_refresh(timeline.follow_tail_if_enabled)

    async def _append_assistant(self, content: str) -> Markdown:
        await self._mount_timeline(Static(
            "YUAN YE", classes="sender assistant-sender",
        ))
        return await self._append_assistant_segment(content)

    async def _append_assistant_segment(self, content: str) -> Markdown:
        widget = Markdown(content, classes="assistant-message", open_links=False)
        if self._turn_container is not None and self._turn_container.is_mounted:
            await self._mount_turn_widget(widget)
        else:
            await self._mount_timeline(widget)
        return widget

    async def _mount_turn_widget(
        self,
        widget: Static | Markdown | LoopActivityCard,
    ) -> None:
        container = self._turn_container
        if container is None or not container.is_mounted:
            await self._mount_timeline(widget)
            return
        timeline = self.query_one("#timeline", ConversationTimeline)
        should_follow = timeline.follow_tail
        trace = self._turn_trace
        target = (
            trace.detail
            if trace is not None and trace.is_mounted and not trace.collapsed
            else container
        )
        await target.mount(widget)
        if should_follow:
            self.call_after_refresh(timeline.follow_tail_if_enabled)

    async def _append_turn_notice(self, content: str) -> None:
        await self._mount_turn_widget(Static(
            content, classes="tool-message notice-message",
        ))

    async def _append_turn_error(self, content: str) -> None:
        await self._mount_turn_widget(Static(
            content, classes="tool-message error-message",
        ))

    async def _replace_turn_with_final(self, answer: str) -> None:
        """Collapse the complete transient trace and append the canonical answer."""
        self._flush_stream()
        container = self._turn_container
        if container is None or not container.is_mounted:
            self._assistant_text = answer
            self._assistant = await self._append_assistant(answer)
            return
        # The provider's final streamed segment is commonly byte-for-byte the
        # canonical final answer. Keeping it inside the expandable trace would
        # show the same answer twice. Preserve only genuinely intermediate text.
        streamed = self._assistant
        if (
            streamed is not None
            and streamed.parent is not None
            and self._assistant_text.strip() == answer.strip()
        ):
            await streamed.remove()
            self._assistant = None
        # A direct answer still exposes a static process summary, while the
        # transient spinner is stopped before the completed Turn is published.
        activity = self._current_activity
        if activity is not None and not activity.tools and activity.is_mounted:
            activity.finish_loop()
        for card in self._activity_cards.values():
            card.settle_pending_tools()
            card.finish_loop()
        trace = self._turn_trace
        if trace is not None:
            started = self._turn_started_at
            trace.complete(
                monotonic() - started if started is not None else 0.0,
                tool_names=[
                    str(item.get("name") or "")
                    for card in self._activity_cards.values()
                    for item in card.tools
                ],
                has_process=True,
            )
        self._assistant_text = answer
        final = Markdown(
            answer or "…", classes="assistant-message", open_links=False,
        )
        await container.mount(final)
        self._assistant = final
        self._activity_cards.clear()
        self._current_activity = None
        self._follow_timeline()

    def _resume_thinking_after_tools(
        self,
        card: LoopActivityCard,
        epoch: int,
    ) -> None:
        if (
            epoch == self._activity_epoch
            and card is self._current_activity
            and not any(item.get("status") == "running" for item in card.tools)
        ):
            card.show_thinking()

    def _move_activity_to_turn_tail(
        self,
        card: LoopActivityCard | None = None,
    ) -> None:
        """Keep the compact current-status row at the live edge of the turn."""
        selected = card or self._current_activity
        trace = self._turn_trace
        container = trace.detail if trace is not None else self._turn_container
        if (
            selected is None
            or container is None
            or selected.parent is not container
            or not container.children
            or container.children[-1] is selected
        ):
            return
        container.move_child(selected, after=container.children[-1])

    async def _append_notice(self, content: str) -> None:
        await self._mount_timeline(Static(
            content, classes="tool-message notice-message",
        ))

    async def _append_error(self, content: str) -> None:
        await self._mount_timeline(Static(
            content, classes="tool-message error-message",
        ))

    async def _mount_timeline(
        self, widget: Static | Markdown | LoopActivityCard | Vertical,
    ) -> None:
        timeline = self.query_one("#timeline", ConversationTimeline)
        should_follow = timeline.follow_tail
        await timeline.mount(widget)
        if should_follow:
            self.call_after_refresh(timeline.follow_tail_if_enabled)

    def _follow_timeline(self) -> None:
        # Markdown may finish parsing after the app has begun unmounting.  Its
        # completion message must not resurrect or fail a closed TUI session.
        try:
            timeline = self.query_one("#timeline", ConversationTimeline)
        except NoMatches:
            return
        if timeline.follow_tail:
            self.call_after_refresh(timeline.follow_tail_if_enabled)

    @on(Markdown.TableOfContentsUpdated, ".assistant-message")
    def _assistant_document_mounted(self) -> None:
        # Markdown.update() parses and mounts asynchronously. Follow after
        # those blocks exist and layout can compute their final scroll range.
        self._follow_timeline()
        self.set_timer(0.05, self._follow_timeline)

    def _flush_stream(self) -> None:
        if not self._stream_dirty or self._assistant is None:
            return
        self._assistant.update(self._assistant_text)
        self._stream_dirty = False
        self._follow_timeline()
        # The Markdown document's block widgets and final height settle after
        # update() returns. A second guarded follow avoids pinning to the old
        # maximum while still respecting a wheel-up that happened meanwhile.
        self.set_timer(0.05, self._follow_timeline)

    @staticmethod
    def _is_local_command(value: str) -> bool:
        command = value.split(maxsplit=1)[0]
        return command in {
            "/code", "/skill", "/inbox", "/tool-result", "/cron",
            "/dream", "/harness", "/extension", "/reload",
        }

    def _set_busy(self, busy: bool, status: str) -> None:
        composer = self.query_one("#composer", Input)
        composer.disabled = busy
        self.query_one("#reasoning-switch", Select).disabled = busy
        if busy:
            self._clear_ctrl_c_confirmation()
            self._hide_command_menu()
        else:
            self._set_composer_notice(None)
        self._set_status(status)
        if not busy:
            composer.focus()

    def _set_status(self, status: str) -> None:
        self._current_status = status
        suffix = f" · {self.unread_count} 通知" if self.unread_count else ""
        widget = self.query_one("#run-status", Static)
        widget.remove_class("status-running", "status-complete", "status-error")
        if status in {"连接 Gateway", "运行中"} or status.startswith("执行 "):
            widget.add_class("status-running")
        elif status == "已完成":
            widget.add_class("status-complete")
        elif status in {"执行失败", "已取消"}:
            widget.add_class("status-error")
        widget.update(f"{status}{suffix}")
        self._update_brand()

    def _update_brand(self) -> None:
        if self._data_mode is not None:
            self.query_one("#brand", Static).update(self._data_mode.upper())
            self.query_one("#session-context", Static).update(
                "DATA VIEW  /  FULL WIDTH  /",
            )
            self.query_one("#model-switch", Button).label = self._model_name or "model"
            return
        if self._code_mode:
            branch = str(getattr(self._code_session, "branch", "isolated"))
            self.query_one("#session-context", Static).update(
                f"ISOLATED WORKTREE  /  {branch}  /",
            )
            self.query_one("#model-switch", Button).label = self._model_name or "model"
            return
        session = self.session_id[:12] if self.session_id else "新会话"
        self.query_one("#session-context", Static).update(
            f"LOCAL GATEWAY  /  {session}  /",
        )
        self.query_one("#model-switch", Button).label = self._model_name or "model"

    def action_cycle_model(self) -> None:
        if self._worker_running or not self._model_options:
            return
        current = next(
            (
                index for index, item in enumerate(self._model_options)
                if item["profile_id"] == self._model_profile_id
            ),
            -1,
        )
        self._select_model(current + 1)

    @property
    def _worker_running(self) -> bool:
        return (
            self._active_worker is not None
            and self._active_worker.state
            not in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}
        )

    def action_interrupt_or_exit(self) -> None:
        if self._data_mode is not None:
            if self._worker_running and self._active_worker is not None:
                self._active_worker.cancel()
            self._leave_data_mode()
            return
        if self._code_mode:
            now = monotonic()
            if now - self._last_ctrl_c <= 2:
                if self._worker_running and self._active_worker is not None:
                    self._active_worker.cancel()
                self._clear_ctrl_c_confirmation()
                self._leave_code_mode(preserved=True)
            else:
                self._last_ctrl_c = now
                message = (
                    "再按一次 Ctrl+C 停止等待并返回；Gateway 会保留 Coding 记录"
                    if self._worker_running else
                    "再按一次 Ctrl+C 保留 Coding Session 并返回 Main Agent"
                )
                self._set_composer_notice(message)
                self.set_timer(2, self._expire_ctrl_c_confirmation)
            return
        if self._worker_running:
            self._clear_ctrl_c_confirmation()
            self._active_worker.cancel()
            self._set_status("正在取消")
            self._set_composer_notice("正在取消当前回答…")
            return
        now = monotonic()
        if now - self._last_ctrl_c <= 2:
            self.exit(self.session_id)
        else:
            self._last_ctrl_c = now
            self._set_composer_notice("再按一次 Ctrl+C 退出客户端")
            self.set_timer(2, self._expire_ctrl_c_confirmation)

    def _expire_ctrl_c_confirmation(self) -> None:
        if self._last_ctrl_c and monotonic() - self._last_ctrl_c >= 2:
            self._clear_ctrl_c_confirmation()

    def _clear_ctrl_c_confirmation(self) -> None:
        self._last_ctrl_c = 0.0
        self._set_composer_notice(None)

    def _set_composer_notice(self, message: str | None) -> None:
        composer = self.query_one("#composer", Input)
        composer.border_title = message
        # Keep the notice in one stable place. Mirroring it into the placeholder
        # renders the same sentence twice whenever the composer is empty.
        composer.placeholder = (
            "输入 Coding 需求，或使用 /exit、/abort"
            if self._code_mode else
            f"输入 /{self._data_mode} 子命令，或 /exit 返回 Main Agent"
            if self._data_mode else _COMPOSER_PLACEHOLDER
        )

    def action_exit_chat(self) -> None:
        if self._worker_running:
            self._active_worker.cancel()
        self.exit(self.session_id)

    def action_focus_composer(self) -> None:
        self.query_one("#composer", Input).focus()

    def action_history_up(self) -> None:
        if self._data_mode is not None:
            self.query_one("#data-view", VerticalScroll).scroll_page_up()
            return
        timeline = self.query_one(
            "#code-timeline" if self._code_mode else "#timeline",
            ConversationTimeline,
        )
        timeline.follow_tail = False
        timeline.scroll_page_up()

    def action_history_down(self) -> None:
        if self._data_mode is not None:
            self.query_one("#data-view", VerticalScroll).scroll_page_down()
            return
        timeline = self.query_one(
            "#code-timeline" if self._code_mode else "#timeline",
            ConversationTimeline,
        )
        timeline.scroll_page_down()
        self.call_after_refresh(
            lambda: setattr(
                timeline,
                "follow_tail",
                timeline.scroll_target_y >= timeline.max_scroll_y - 1,
            )
        )


__all__ = [
    "BrailleSpinner", "COMMANDS", "ConfirmModal", "ConversationTimeline",
    "CorrectionModal", "LoopActivityCard", "YuanYeChatApp",
]
