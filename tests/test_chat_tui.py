from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from textual.containers import VerticalScroll
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widgets import Button, Input, Markdown, OptionList, Select, Static
from textual.worker import WorkerState

from gateway.models import GatewayEventEnvelope
from run_ui.chat_tui import (
    BrailleSpinner,
    ConversationTimeline,
    LoopActivityCard,
    TurnTraceDisclosure,
    YuanYeChatApp,
    _COMPOSER_PLACEHOLDER,
    _format_elapsed,
)


class _Client:
    def __init__(self) -> None:
        self.acknowledged: list[str] = []

    async def start_run(
        self, project_id, prompt, session_id, model_profile_id="default",
        reasoning_effort=None,
    ):
        assert (project_id, prompt) == ("project", "hello")
        assert session_id == "session"
        assert model_profile_id == "default"
        assert reasoning_effort == "low"
        return SimpleNamespace(run_id="run-1")

    async def subscribe(self, run_id):
        assert run_id == "run-1"
        for sequence, event_type, payload in [
            (1, "reasoning", {"content": "简短判断", "loop": 1}),
            (2, "reasoning", {"content": "后直接回答。", "loop": 1}),
            (3, "text", {"content": "streamed "}),
            (4, "text", {"content": "answer"}),
            (5, "observer_progress", {"progress_markdown": "正在核对意图"}),
            (6, "final", {
                "answer": "streamed answer", "reasoning": "简短判断后直接回答。",
            }),
            (7, "run_completed", {"answer": "streamed answer"}),
        ]:
            yield GatewayEventEnvelope(
                event_id=f"event-{sequence}",
                sequence=sequence,
                timestamp="2026-09-14T00:00:00+08:00",
                project_id="project",
                session_id="session",
                run_id=run_id,
                type=event_type,
                payload=payload,
            )

    async def acknowledge_run_result(self, run_id):
        self.acknowledged.append(run_id)

    async def observer_status(self, run_id):
        assert run_id == "run-1"
        return {"status": "finalized", "progress_markdown": "✓ 任务已完成"}


class _ToolClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.activity_ready = asyncio.Event()
        self.release_tools = asyncio.Event()
        self.batch_ready = asyncio.Event()
        self.release_final = asyncio.Event()

    async def subscribe(self, run_id):
        events = [
            (1, "text", {"content": "intermediate explanation"}),
            (2, "tool_requested", {
                "name": "read_file", "arguments": {"path": "README.md"},
                "tool_call_id": "call-read", "loop": 1,
                "execution": "parallel", "position": 0,
            }),
            (3, "tool_requested", {
                "name": "search_workspace", "arguments": {"query": "runtime"},
                "tool_call_id": "call-search", "loop": 1,
                "execution": "parallel", "position": 1,
            }),
        ]
        for sequence, event_type, payload in events:
            yield GatewayEventEnvelope(
                event_id=f"tool-event-{sequence}", sequence=sequence,
                timestamp="2026-09-14T00:00:00+08:00", project_id="project",
                session_id="session", run_id=run_id, type=event_type,
                payload=payload,
            )
        self.activity_ready.set()
        await self.release_tools.wait()
        completed = [
            (4, "tool_completed", {
                "name": "read_file", "content": "file body", "status": "success",
                "tool_call_id": "call-read", "position": 0,
                "observation_id": "observation-1", "loop": 1,
                "execution": "parallel",
            }),
            (5, "tool_completed", {
                "name": "search_workspace", "content": "two matches",
                "status": "success", "tool_call_id": "call-search",
                "position": 1, "observation_id": "observation-2", "loop": 1,
                "execution": "parallel",
            }),
            (6, "tool_batch_completed", {
                "tool_call_count": 2, "success_count": 2,
                "failure_count": 0,
            }),
        ]
        for sequence, event_type, payload in completed:
            yield GatewayEventEnvelope(
                event_id=f"tool-event-{sequence}", sequence=sequence,
                timestamp="2026-09-14T00:00:00+08:00", project_id="project",
                session_id="session", run_id=run_id, type=event_type,
                payload=payload,
            )
        self.batch_ready.set()
        await self.release_final.wait()
        for sequence, event_type, payload in [
            (7, "text", {"content": "draft final"}),
            (8, "run_completed", {"answer": "authoritative final"}),
        ]:
            yield GatewayEventEnvelope(
                event_id=f"tool-event-{sequence}", sequence=sequence,
                timestamp="2026-09-14T00:00:00+08:00", project_id="project",
                session_id="session", run_id=run_id, type=event_type,
                payload=payload,
            )


class _FastToolClient(_Client):
    """Emit a complete Tool round-trip before Textual can paint another frame."""

    async def subscribe(self, run_id):
        for sequence, event_type, payload in [
            (1, "tool_requested", {
                "name": "current_time", "arguments": {},
                "tool_call_id": "call-time", "loop": 1,
                "execution": "parallel", "position": 0,
            }),
            (2, "tool_completed", {
                "name": "current_time", "content": "23:00", "status": "success",
                "tool_call_id": "call-time", "position": 0,
                "observation_id": "observation-time", "loop": 1,
                "execution": "parallel",
            }),
            (3, "tool_batch_completed", {
                "tool_call_count": 1, "success_count": 1, "failure_count": 0,
            }),
            (4, "text", {"content": "现在是 23:00。"}),
            (5, "run_completed", {"answer": "现在是 23:00。"}),
        ]:
            yield GatewayEventEnvelope(
                event_id=f"fast-tool-event-{sequence}", sequence=sequence,
                timestamp="2026-09-14T23:00:00+08:00", project_id="project",
                session_id="session", run_id=run_id, type=event_type,
                payload=payload,
            )


class _ApprovalClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.approval_visible = asyncio.Event()
        self.decision: tuple[str, bool] | None = None

    async def subscribe(self, run_id):
        yield GatewayEventEnvelope(
            event_id="bash-requested", sequence=1,
            timestamp="2026-09-15T00:00:00+08:00", project_id="project",
            session_id="session", run_id=run_id, type="tool_requested",
            payload={
                "name": "bash", "arguments": {"command": "ls"},
                "tool_call_id": "call-bash", "loop": 1,
                "execution": "serial", "position": 0,
            },
        )
        self.approval_visible.set()
        yield GatewayEventEnvelope(
            event_id="approval-event", sequence=2,
            timestamp="2026-09-15T00:00:00+08:00", project_id="project",
            session_id="session", run_id=run_id, type="approval_requested",
            payload={
                "approval_id": "approval-1", "tool_name": "bash",
                "arguments": {"command": "ls"},
            },
        )
        while self.decision is None:
            await asyncio.sleep(0.01)
        yield GatewayEventEnvelope(
            event_id="bash-completed", sequence=3,
            timestamp="2026-09-15T00:00:01+08:00", project_id="project",
            session_id="session", run_id=run_id, type="tool_completed",
            payload={
                "name": "bash", "content": "command completed",
                "status": "success" if self.decision[1] else "skipped",
                "tool_call_id": "call-bash", "position": 0,
                "observation_id": "observation-bash", "loop": 1,
                "execution": "serial",
            },
        )
        yield GatewayEventEnvelope(
            event_id="approval-complete", sequence=4,
            timestamp="2026-09-15T00:00:01+08:00", project_id="project",
            session_id="session", run_id=run_id, type="run_completed",
            payload={"answer": "done"},
        )

    async def respond_approval(self, approval_id, approved):
        self.decision = (approval_id, approved)
        return approved


class _WaitingClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def subscribe(self, run_id):
        self.started.set()
        await self.release.wait()
        yield GatewayEventEnvelope(
            event_id="waiting-complete", sequence=1,
            timestamp="2026-09-15T00:00:01+08:00", project_id="project",
            session_id="session", run_id=run_id, type="run_completed",
            payload={"answer": "done"},
        )


class _CacheUsageClient(_Client):
    def __init__(self, *, reported: bool) -> None:
        super().__init__()
        self.reported = reported

    async def subscribe(self, run_id):
        cache = {
            "status": "reported" if self.reported else "unavailable",
            "hit_tokens": 75 if self.reported else None,
            "miss_tokens": 25 if self.reported else None,
            "total_tokens": 100 if self.reported else None,
            "hit_ratio": 0.75 if self.reported else None,
            "source": "deepseek.prompt_cache_tokens" if self.reported else None,
        }
        for sequence, event_type, payload in [
            (1, "model_usage", {
                "model": {"provider": "deepseek", "name": "deepseek-chat"},
                "model_call": {"prefix_cache": cache},
            }),
            (2, "run_completed", {"answer": "done"}),
        ]:
            yield GatewayEventEnvelope(
                event_id=f"cache-event-{sequence}", sequence=sequence,
                timestamp="2026-09-15T00:00:01+08:00", project_id="project",
                session_id="session", run_id=run_id, type=event_type,
                payload=payload,
            )


class _SelectedModelClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.selected_profile: str | None = None
        self.selected_reasoning_effort: str | None = None

    async def start_run(
        self, project_id, prompt, session_id, model_profile_id="default",
        reasoning_effort=None,
    ):
        self.selected_profile = model_profile_id
        self.selected_reasoning_effort = reasoning_effort
        return SimpleNamespace(run_id="run-1", session_id=session_id)


class _CodeClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.events_sent = False
        self.selected_profile = ""
        self.selected_reasoning_effort = ""

    async def start_code_session(self, project_id, origin_session_id=None):
        assert (project_id, origin_session_id) == ("project", "session")
        return SimpleNamespace(code_session_id="code-1", branch="yy/code-1")

    async def run_code_turn(
        self, session_id, task, *, model_profile_id="default", reasoning_effort=None,
    ):
        assert (session_id, task) == ("code-1", "add a tool")
        self.selected_profile = model_profile_id
        self.selected_reasoning_effort = reasoning_effort
        await asyncio.sleep(0.02)
        return SimpleNamespace(
            status="verified", message="扩展已通过验证", test_file="tests/test_tool.py",
            attempts=1, commit="abc123", diagnostic="all checks passed",
            model_calls=(),
        )

    async def code_session_events(self, session_id, after_sequence=0):
        assert session_id == "code-1"
        if self.events_sent or after_sequence:
            return []
        self.events_sent = True
        return [{"sequence": 1, "record_type": "code_generation"}]

    async def finalize_code_session(self, session_id, approved_plan_hash=None):
        assert session_id == "code-1"
        return SimpleNamespace(
            status="merged", message="已合并", stay_in_code_mode=False,
            grant_plan={},
        )

    async def abort_code_session(self, session_id):
        assert session_id == "code-1"
        return SimpleNamespace(message="已放弃", stay_in_code_mode=False)


class _SlowCodeClient(_CodeClient):
    def __init__(self) -> None:
        super().__init__()
        self.code_started = asyncio.Event()
        self.code_release = asyncio.Event()

    async def run_code_turn(self, session_id, task, **kwargs):
        self.code_started.set()
        await self.code_release.wait()
        return await super().run_code_turn(session_id, task, **kwargs)


async def _external(command: str, session_id: str | None) -> None:
    del command, session_id


def test_chat_tui_is_one_persistent_scrollable_session_surface() -> None:
    async def check() -> None:
        displayed = 0

        async def history_displayed() -> None:
            nonlocal displayed
            displayed += 1

        app = YuanYeChatApp(
            _Client(),
            "project",
            session_id="session",
            records=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
            external_command=_external,
            history_displayed=history_displayed,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            timeline = app.query_one("#timeline", VerticalScroll)
            observer = app.query_one("#observer-pane", VerticalScroll)
            assert displayed == 1
            assert timeline.region.width > observer.region.width
            assert app.query_one("#composer", Input).has_focus
            assert len(app.query(".user-message")) == 1
            assert len(app.query(".assistant-message")) == 1
            trace = app.query_one(TurnTraceDisclosure)
            assert trace.collapsed is True
            assert "用时" in str(trace.header.render())
            assert "查看过程" in str(trace.header.render())

    asyncio.run(check())


def test_external_command_output_is_rendered_inside_timeline() -> None:
    async def check() -> None:
        async def external(command: str, session_id: str | None) -> str:
            assert command == "/inbox"
            assert session_id == "session"
            return "Inbox 暂无结果。"

        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_external_command("/inbox")
            await worker.wait()
            await pilot.pause()
            assert app._data_mode == "inbox"
            assert app.query_one("#timeline", ConversationTimeline).display is False
            assert app.query_one("#observer-pane", VerticalScroll).display is False
            assert app.query_one("#data-view", VerticalScroll).display is True
            assert "Inbox 暂无结果" in str(
                app.query_one("#data-content", Static).render()
            )
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app._data_mode is None
            assert app.query_one("#timeline", ConversationTimeline).display is True

    asyncio.run(check())


def test_long_data_table_scrolls_with_mouse_wheel() -> None:
    async def check() -> None:
        async def external(command: str, session_id: str | None) -> str:
            return "\n".join(f"table row {index}" for index in range(100))

        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=external,
        )
        async with app.run_test(size=(100, 28)) as pilot:
            worker = app.run_external_command("/inbox")
            await worker.wait()
            await pilot.pause(0.1)
            view = app.query_one("#data-view", VerticalScroll)
            content = app.query_one("#data-content", Static)
            assert view.max_scroll_y > 0
            assert view.scroll_y == 0
            await pilot._post_mouse_events(
                [MouseScrollDown], content, offset=(10, 5),
            )
            await pilot.pause()
            assert view.scroll_y > 0

    asyncio.run(check())


def test_data_view_accepts_subcommands_and_exit_without_polluting_main() -> None:
    async def check() -> None:
        commands: list[str] = []

        async def external(command: str, session_id: str | None) -> str:
            commands.append(command)
            return f"result for {command}"

        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/inbox"
            await pilot.press("enter")
            if app._active_worker is not None:
                await app._active_worker.wait()
            composer.value = "/inbox show abc123"
            await pilot.press("enter")
            if app._active_worker is not None:
                await app._active_worker.wait()
            assert commands == ["/inbox", "/inbox show abc123"]
            assert "show abc123" in str(app.query_one("#data-content", Static).render())
            assert not list(app.query_one("#timeline").query(".notice-message"))
            composer.value = "/exit"
            await pilot.press("enter")
            await pilot.pause()
            assert app._data_mode is None

    asyncio.run(check())


def test_external_command_width_tracks_timeline_content() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            wide = app.command_output_width()
            await pilot.resize_terminal(80, 34)
            await pilot.pause()
            narrow = app.command_output_width()
            assert 32 <= narrow < wide

    asyncio.run(check())


def test_code_mode_stays_inside_tui_and_double_ctrl_c_returns_to_chat() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _CodeClient(), "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_external_command("/code")
            await worker.wait()
            await pilot.pause()
            assert app._code_mode is True
            assert "YY CODE" in str(app.query_one("#brand", Static).render())
            assert app.query_one("#timeline", ConversationTimeline).display is False
            assert app.query_one("#code-timeline", ConversationTimeline).display is True
            assert "CODE OBSERVER" in str(
                app.query_one("#observer-heading", Static).render(),
            )

            composer = app.query_one("#composer", Input)
            composer.value = "add a tool"
            await pilot.press("enter")
            if app._active_worker is not None:
                await app._active_worker.wait()
            await pilot.pause()
            code_timeline = app.query_one("#code-timeline", ConversationTimeline)
            assert any(
                "扩展已通过验证" in str(item._markdown)
                for item in code_timeline.query(".assistant-message")
            )
            assert any(
                "Coding Agent 正在生成代码" in str(item.render())
                for item in code_timeline.query(".notice-message")
            )

            await pilot.press("ctrl+c", "ctrl+c")
            await pilot.pause()
            assert app._code_mode is False
            assert app._code_session is not None
            assert "YUAN YE" in str(app.query_one("#brand", Static).render())
            assert app.query_one("#timeline", ConversationTimeline).display is True

    asyncio.run(check())


def test_code_mode_uses_selected_model_and_its_own_cache_session() -> None:
    class CodeCacheClient(_CodeClient):
        async def run_code_turn(self, session_id, task, **kwargs):
            result = await super().run_code_turn(session_id, task, **kwargs)
            result.model_calls = ({
                "prefix_cache": {
                    "status": "reported",
                    "hit_tokens": 50,
                    "total_tokens": 100,
                },
            },)
            return result

    async def check() -> None:
        client = CodeCacheClient()
        options = (
            SimpleNamespace(
                profile_id="default", provider="openai", model="main-model",
                selected=True, reasoning_effort="low",
            ),
            SimpleNamespace(
                profile_id="flash", provider="openai", model="code-model",
                selected=False, reasoning_effort="high",
            ),
        )
        records = [{
            "model_calls": [{
                "prefix_cache": {
                    "status": "reported",
                    "hit_tokens": 90,
                    "total_tokens": 100,
                },
            }],
        }]
        app = YuanYeChatApp(
            client, "project", session_id="session", records=records,
            external_command=_external, model_options=options,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_external_command("/code")
            await worker.wait()
            await pilot.pause()
            assert app.query_one("#model-switch", Button).disabled is False
            assert app.query_one("#reasoning-switch", Select).disabled is False
            assert "??.?%" in str(app.query_one("#cache-status", Static).render())
            app._select_model(1)
            app._reasoning_effort = "high"
            composer = app.query_one("#composer", Input)
            composer.value = "add a tool"
            await pilot.press("enter")
            if app._active_worker is not None:
                await app._active_worker.wait()
            await pilot.pause()
            assert client.selected_profile == "flash"
            assert client.selected_reasoning_effort == "high"
            assert "50.0%" in str(app.query_one("#cache-status", Static).render())
            await pilot.press("ctrl+c", "ctrl+c")
            await pilot.pause()
            assert "90.0%" in str(app.query_one("#cache-status", Static).render())

    asyncio.run(check())


def test_double_ctrl_c_can_detach_an_active_code_turn() -> None:
    async def check() -> None:
        client = _SlowCodeClient()
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_external_command("/code")
            await worker.wait()
            composer = app.query_one("#composer", Input)
            composer.value = "add a tool"
            await pilot.press("enter")
            await asyncio.wait_for(client.code_started.wait(), timeout=2)
            await pilot.press("ctrl+c", "ctrl+c")
            await pilot.pause()
            assert app._code_mode is False
            assert app._code_session is not None
            client.code_release.set()
            await pilot.pause()

    asyncio.run(check())


def test_fast_tool_call_remains_visible_in_completed_turn_summary() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _FastToolClient(), "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_agent("hello")
            await worker.wait()
            await pilot.pause()
            trace = app.query_one(TurnTraceDisclosure)
            header = str(trace.header.render())
            assert trace.collapsed is True
            assert "工具 current_time" in header
            assert "查看过程" in header
            turn = app._turn_container
            assert turn is not None
            answers = list(turn.query(".assistant-message"))
            assert len(answers) == 1
            assert list(turn.children).index(trace) < list(turn.children).index(answers[0])
            trace.header.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(trace.header, offset=(2, 0))
            await pilot.pause()
            assert "current_time" in str(
                trace.query_one(LoopActivityCard).detail.render()
            )

    asyncio.run(check())


def test_durable_approval_event_replaces_composer_and_submits_mouse_decision() -> None:
    async def check() -> None:
        client = _ApprovalClient()
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_agent("hello")
            await asyncio.wait_for(client.approval_visible.wait(), timeout=2)
            for _ in range(20):
                await pilot.pause(0.01)
                if app.query_one("#approval-bar").display:
                    break
            assert app.query_one("#approval-bar").display is True
            assert app.query_one("#composer", Input).display is False
            prompt = str(app.query_one("#approval-prompt", Static).render())
            assert "bash" in prompt
            assert "ls" in prompt
            assert await pilot.click("#approval-allow")
            await worker.wait()
            assert client.decision == ("approval-1", True)
            assert app.query_one("#approval-bar").display is False
            assert app.query_one("#composer", Input).display is True
            trace = app.query_one(TurnTraceDisclosure)
            assert "工具 bash" in str(trace.header.render())
            assert await pilot.click(trace.header, offset=(2, 0))
            await pilot.pause()
            card = trace.query_one(LoopActivityCard)
            assert card.collapsed is False
            detail = str(card.detail.render())
            assert "bash" in detail
            assert "ls" in detail
            assert "command completed" in detail

    asyncio.run(check())


def test_inline_approval_supports_left_right_and_enter() -> None:
    async def check() -> None:
        client = _ApprovalClient()
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_agent("hello")
            await asyncio.wait_for(client.approval_visible.wait(), timeout=2)
            for _ in range(20):
                await pilot.pause(0.01)
                if app.query_one("#approval-bar").display:
                    break
            await pilot.press("right", "enter")
            await worker.wait()
            assert client.decision == ("approval-1", False)

    asyncio.run(check())


def test_inline_approval_has_complete_layout_labels_and_strong_focus() -> None:
    async def check() -> None:
        client = _ApprovalClient()
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_agent("hello")
            await asyncio.wait_for(client.approval_visible.wait(), timeout=2)
            for _ in range(20):
                await pilot.pause(0.01)
                if app.query_one("#approval-bar").display:
                    break
            await pilot.pause()
            allow = app.query_one("#approval-allow")
            deny = app.query_one("#approval-deny")
            assert str(allow.label) == "允许"
            assert str(deny.label) == "拒绝"
            assert allow.has_focus
            assert allow.styles.background != deny.styles.background
            assert app.query_one("#composer-shell").outer_size.height == 5
            assert app.query_one("#approval-bar").outer_size.height == 4
            rendered = "\n".join(
                strip.text for strip in app.screen._compositor.render_strips()
            )
            assert "允许" in rendered
            assert "拒绝" in rendered
            await pilot.press("right")
            assert deny.has_focus
            assert deny.styles.background != allow.styles.background
            await pilot.click("#approval-deny")
            await worker.wait()

    asyncio.run(check())


def test_tool_detail_treats_status_and_result_as_plain_text() -> None:
    card = LoopActivityCard(loop=1)
    card.start_tool({
        "name": "bash", "arguments": {"command": "pwd"},
        "tool_call_id": "call-pwd", "position": 0,
        "execution": "serial", "loop": 1,
    })
    card.complete_tool({
        "name": "bash", "content": "Path\r\n----\r\nYYWorkspace:\\\n",
        "status": "success", "tool_call_id": "call-pwd", "position": 0,
        "observation_id": "observation-pwd", "execution": "serial", "loop": 1,
    })
    rendered = str(card.detail.render())
    assert "[success]" in rendered
    assert "YYWorkspace:\\" in rendered
    assert rendered.index("结果:") < rendered.index("参数:")


def test_thinking_label_and_spinner_share_one_centered_header() -> None:
    card = LoopActivityCard()
    assert card.title == "思考中"
    assert "content-align: center middle;" in YuanYeChatApp.CSS


def test_restored_completed_turn_keeps_clickable_tool_trace() -> None:
    async def check() -> None:
        records = [
            {"role": "user", "content": "检查文件", "timestamp": "2026-09-14 10:00:00"},
            {
                "role": "assistant", "content": "我先检查。",
                "timestamp": "2026-09-14 10:00:01",
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }],
            },
            {
                "role": "tool", "name": "read_file", "tool_call_id": "call-1",
                "status": "success", "content": "body", "record_id": "observation-1",
                "timestamp": "2026-09-14 10:00:02",
            },
            {"role": "assistant", "content": "检查完成。", "timestamp": "2026-09-14 10:00:03"},
        ]
        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=records,
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            trace = app.query_one(TurnTraceDisclosure)
            assert trace.collapsed is True
            assert "3s" in str(trace.header.render())
            await pilot.click(trace.header, offset=(2, 0))
            await pilot.pause()
            assert trace.collapsed is False
            card = trace.query_one(LoopActivityCard)
            assert card.collapsed is False
            assert "README.md" in str(card.detail.render())
            assert "observation-1" in str(card.detail.render())
            await pilot.click(card.header, offset=(2, 0))
            await pilot.pause()
            assert card.collapsed is True

    asyncio.run(check())


def test_completed_turn_elapsed_uses_compact_hours_minutes_seconds() -> None:
    assert _format_elapsed(8.2) == "8s"
    assert _format_elapsed(12 * 60 + 17) == "12m 17s"
    assert _format_elapsed(60 * 60 + 2 * 60 + 3) == "1h 2m 3s"
    assert _format_elapsed(-1) == "0s"


def test_chat_tui_streams_without_rebuilding_the_session_surface() -> None:
    async def check() -> None:
        client = _Client()
        app = YuanYeChatApp(
            client,
            "project",
            session_id="session",
            records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_agent("hello")
            await worker.wait()
            await pilot.pause()
            app._flush_stream()
            assert app._assistant_text == "streamed answer"
            assert client.acknowledged == ["run-1"]
            assert "任务已完成" in str(
                app.query_one("#observer-progress", Markdown)._markdown,
            )
            assert len(app.query("#workspace")) == 1
            turn = app._turn_container
            assert turn is not None
            assert len(turn.query(".assistant-message")) == 1
            trace = turn.query_one(TurnTraceDisclosure)
            assert "查看过程" in str(trace.header.render())
            trace.header.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(trace.header, offset=(2, 0))
            await pilot.pause()
            card = trace.query_one(LoopActivityCard)
            assert card.spinner.display is False
            assert "简短判断后直接回答。" in str(card.detail.render())
            assert "本轮未调用工具" in str(card.detail.render())

    asyncio.run(check())


def test_terminal_run_never_restores_stale_active_observer_progress() -> None:
    class ActiveObserverClient(_Client):
        async def observer_status(self, run_id):
            assert run_id == "run-1"
            return {
                "status": "active",
                "progress_markdown": "## 进行中\n\n- 仍在处理用户问题",
            }

    async def check() -> None:
        app = YuanYeChatApp(
            ActiveObserverClient(), "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            progress = app.query_one("#observer-progress", Markdown)
            progress.update("## 进行中\n\n- 仍在处理用户问题")
            await app._consume_gateway_event(GatewayEventEnvelope(
                event_id="event-terminal",
                sequence=1,
                timestamp="2026-09-15T00:00:00+08:00",
                project_id="project",
                session_id="session",
                run_id="run-1",
                type="run_completed",
                payload={"answer": "完成"},
            ))
            await app._finish_observer("run-1", timeout_seconds=0)
            await app._consume_gateway_event(GatewayEventEnvelope(
                event_id="event-late-observer",
                sequence=2,
                timestamp="2026-09-15T00:00:01+08:00",
                project_id="project",
                session_id="session",
                run_id="run-1",
                type="observer_progress",
                payload={"progress_markdown": "## 进行中\n\n- 延迟旧状态"},
            ))
            await pilot.pause()
            rendered = str(progress._markdown)
            assert "任务已完成" in rendered
            assert "仍在处理用户问题" not in rendered
            assert "延迟旧状态" not in rendered

    asyncio.run(check())


def test_chat_tui_runtime_slash_commands_still_go_through_gateway_run() -> None:
    assert YuanYeChatApp._is_local_command("/inbox") is True
    assert YuanYeChatApp._is_local_command("/cron status") is True
    assert YuanYeChatApp._is_local_command("/compress") is False
    assert YuanYeChatApp._is_local_command("/context refresh") is False


def test_timeline_suspends_follow_on_scroll_and_resumes_at_bottom() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session",
            records=[
                {"role": "assistant", "content": f"history {index}\n" * 3}
                for index in range(40)
            ],
            external_command=_external,
        )
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause(0.1)
            timeline = app.query_one("#timeline", ConversationTimeline)
            assert timeline.follow_tail is True
            assert timeline.scroll_target_y >= timeline.max_scroll_y - 1
            timeline.scroll_home(animate=False)
            await pilot.pause()
            assert timeline.follow_tail is False
            previous_target = timeline.scroll_target_y
            await app._append_notice("new background update")
            await pilot.pause()
            assert timeline.scroll_target_y == previous_target
            timeline.scroll_end(animate=False)
            await pilot.pause()
            assert timeline.follow_tail is True

    asyncio.run(check())


def test_submitting_new_prompt_returns_timeline_to_live_bottom() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session",
            records=[
                {"role": "assistant", "content": f"history {index}\n" * 4}
                for index in range(50)
            ],
            external_command=_external,
        )
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause(0.1)
            timeline = app.query_one("#timeline", ConversationTimeline)
            timeline.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            assert timeline.follow_tail is False
            composer = app.query_one("#composer", Input)
            composer.value = "hello"
            await pilot.press("enter")
            if app._active_worker is not None:
                await app._active_worker.wait()
            await pilot.pause(0.1)
            assert timeline.follow_tail is True
            assert timeline.scroll_y >= timeline.max_scroll_y - 1

    asyncio.run(check())


def test_submitted_prompt_is_visible_before_agent_produces_output() -> None:
    async def check() -> None:
        client = _WaitingClient()
        app = YuanYeChatApp(
            client, "project", session_id="session",
            records=[
                {"role": "assistant", "content": f"old history {index}\n" * 4}
                for index in range(50)
            ],
            external_command=_external,
        )
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause(0.1)
            timeline = app.query_one("#timeline", ConversationTimeline)
            timeline.scroll_home(animate=False, immediate=True)
            composer = app.query_one("#composer", Input)
            composer.value = "hello"
            await pilot.press("enter")
            await asyncio.wait_for(client.started.wait(), timeout=2)
            await pilot.pause(0.1)
            assert timeline.follow_tail is True
            assert timeline.scroll_y >= timeline.max_scroll_y - 1
            region = timeline.content_region
            visible = "\n".join(
                strip.crop(region.x, region.right - 2).text
                for strip in app.screen._compositor.render_strips()[region.y:region.bottom]
            )
            assert "hello" in visible
            client.release.set()
            if app._active_worker is not None:
                await app._active_worker.wait()

    asyncio.run(check())


def test_final_collapses_complete_turn_trace_and_keeps_tools_expandable() -> None:
    async def check() -> None:
        client = _ToolClient()
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_agent("hello")
            await asyncio.wait_for(client.activity_ready.wait(), timeout=2)
            await pilot.pause()
            cards = list(app.query(LoopActivityCard))
            assert len(cards) == 1
            assert isinstance(cards[0].spinner, BrailleSpinner)
            assert str(cards[0].spinner.render()) in BrailleSpinner.FRAMES
            assert cards[0].collapsed is True
            assert "正在执行工具" in cards[0].title
            assert "read_file" in cards[0].title
            assert "search_workspace" in cards[0].title
            cards[0].header.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(cards[0].header, offset=(2, 0))
            await pilot.pause()
            assert cards[0].collapsed is False
            detail = str(cards[0].detail.render())
            assert "当前模型未提供可展示的思维链" in detail
            assert "read_file" in detail
            assert "search_workspace" in detail
            cards[0].header.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(cards[0].header, offset=(2, 0))
            await pilot.pause()
            assert cards[0].collapsed is True

            client.release_tools.set()
            await asyncio.wait_for(client.batch_ready.wait(), timeout=2)
            await pilot.pause()
            assert cards[0].title == "思考中"
            assert all(item["status"] != "running" for item in cards[0].tools)
            assert cards[0].spinner.display is True

            client.release_final.set()
            await worker.wait()
            await pilot.pause()
            turn = app._turn_container
            assert turn is not None
            trace = turn.query_one(TurnTraceDisclosure)
            assert trace.collapsed is True
            assert "用时" in str(trace.header.render())
            assert "read_file" in str(trace.header.render())
            answers = list(turn.query(".assistant-message"))
            assert len(answers) == 3
            rendered = str(answers[-1]._markdown)
            assert "authoritative final" in rendered
            assert "intermediate explanation" in str(answers[0]._markdown)
            assert "draft final" in str(answers[1]._markdown)

            trace.header.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(trace.header, offset=(2, 0))
            await pilot.pause()
            assert trace.collapsed is False
            cards = list(trace.query(LoopActivityCard))
            assert len(cards) == 1
            assert cards[0].collapsed is False
            cards[0].header.scroll_visible(animate=False)
            await pilot.pause()
            assert await pilot.click(cards[0].header, offset=(2, 0))
            await pilot.pause()
            assert cards[0].collapsed is True

    asyncio.run(check())


def test_mouse_wheel_suspends_tail_follow_before_stream_refresh() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session",
            records=[
                {"role": "assistant", "content": f"history {index}\n" * 4}
                for index in range(50)
            ],
            external_command=_external,
        )
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause(0.1)
            timeline = app.query_one("#timeline", ConversationTimeline)
            timeline.pin_to_tail()
            await pilot.pause()
            bottom = timeline.scroll_target_y
            content = list(app.query(".assistant-message"))[-1]
            await pilot._post_mouse_events(
                [MouseScrollUp], content, offset=(5, 1),
            )
            await pilot.pause()
            assert timeline.follow_tail is False
            assert timeline.scroll_target_y < bottom
            scrolled_to = timeline.scroll_target_y
            # A stream frame may already have queued a tail callback before
            # the wheel event. It must re-check follow mode when it actually
            # runs instead of snapping back to the bottom.
            app.call_after_refresh(timeline.follow_tail_if_enabled)
            for _ in range(3):
                app._follow_timeline()
                app._flush_stream()
            await pilot.pause()
            assert timeline.scroll_target_y == scrolled_to

    asyncio.run(check())


@pytest.mark.parametrize("navigation", ["wheel", "pageup", "scrollbar", "drag"])
def test_long_answer_navigation_moves_visible_text_and_scrollbar(navigation: str) -> None:
    """A changed scroll target alone does not prove the terminal repainted."""
    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session",
            records=[{
                "role": "assistant",
                "content": "\n\n".join(
                    f"Paragraph {index:03d}: 这是用于验证实际屏幕滚动的长回答。"
                    for index in range(80)
                ),
            }],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause(0.1)
            timeline = app.query_one("#timeline", ConversationTimeline)
            timeline.pin_to_tail()
            await pilot.pause()
            assert timeline.max_scroll_y > timeline.size.height
            region = timeline.content_region
            def visible_body():
                return [
                    strip.crop(region.x, region.right - 2).text
                    for strip in app.screen._compositor.render_strips()[region.y:region.bottom]
                ]
            before = visible_body()
            bottom = timeline.scroll_y
            composer_region = app.query_one("#composer", Input).region
            if navigation == "wheel":
                await pilot._post_mouse_events(
                    [MouseScrollUp], timeline, offset=(10, 5),
                )
            elif navigation == "pageup":
                await pilot.press("pageup")
            elif navigation == "scrollbar":
                assert await pilot.click(timeline.vertical_scrollbar, offset=(0, 2))
            else:
                bar = timeline.vertical_scrollbar
                assert await pilot.mouse_down(bar, offset=(0, bar.size.height - 2))
                await pilot.hover(bar, offset=(0, bar.size.height // 2))
                await pilot.mouse_up(bar, offset=(0, bar.size.height // 2))
            await pilot.pause(0.4)
            after = visible_body()
            assert timeline.scroll_y < bottom
            # Check painted body rows, excluding the scrollbar, header and
            # input. This catches a lost base watch_scroll_y implementation.
            assert before != after, "Scroll offset changed but the visible answer is frozen"
            assert timeline.vertical_scrollbar.position == timeline.scroll_y
            assert timeline.follow_tail is False
            assert app.query_one("#composer", Input).region == composer_region
            await pilot.click("#composer")
            await pilot.press("a")
            assert app.query_one("#composer", Input).value == "a"

    asyncio.run(check())


def test_streaming_long_answer_repaints_on_wheel_and_preserves_reading_position() -> None:
    class LongClient(_ToolClient):
        async def subscribe(self, run_id):
            async for event in super().subscribe(run_id):
                if event.sequence == 1:
                    event = event.model_copy(update={"payload": {"content": "\n\n".join(
                        f"Visible streaming paragraph {index:03d}" for index in range(80)
                    )}})
                yield event

    async def check() -> None:
        client = LongClient()
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            worker = app.run_agent("hello")
            try:
                await asyncio.wait_for(client.activity_ready.wait(), timeout=3)
                # Markdown parses/mounts asynchronously after the text event.
                # Wait for the actual document, not just receipt of the event.
                for _ in range(100):
                    await pilot.pause(0.01)
                    if len(app.query("MarkdownParagraph")) >= 80:
                        break
                assert len(app.query("MarkdownParagraph")) >= 80
                timeline = app.query_one("#timeline", ConversationTimeline)
                await pilot.pause(0.1)
                assert timeline.max_scroll_y > timeline.size.height
                assert timeline.scroll_y >= timeline.max_scroll_y - 1
                region = timeline.content_region
                def visible_body():
                    return [
                        strip.crop(region.x, region.right - 2).text
                        for strip in app.screen._compositor.render_strips()[region.y:region.bottom]
                    ]
                before = visible_body()
                await pilot._post_mouse_events([MouseScrollUp], timeline, offset=(10, 5))
                await pilot.pause()
                reading = visible_body()
                assert before != reading
                position = timeline.scroll_y
                assert not timeline.follow_tail
                # A subsequent streamed segment must not move the reader.
                await app._consume_gateway_event(SimpleNamespace(
                    type="text", payload={"content": "\n\nNew streamed content"},
                ))
                app._flush_stream()
                await pilot.pause()
                assert timeline.scroll_y == position
                assert visible_body() == reading
                assert timeline.vertical_scrollbar.position == position
            finally:
                client.release_tools.set()
                client.release_final.set()
                await worker.wait()
            await pilot.pause()
            assert app.query_one("#composer", Input).disabled is False
            assert "authoritative final" == app._assistant_text

    asyncio.run(check())


def test_ctrl_c_requires_confirmation_when_idle_and_only_cancels_when_busy() -> None:
    class _RunningWorker:
        state = WorkerState.RUNNING

        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "尚未发送的草稿"
            app.action_interrupt_or_exit()
            assert app._last_ctrl_c > 0
            assert app._current_status == "就绪"
            assert str(composer.border_title) == "再按一次 Ctrl+C 退出客户端"
            assert composer.placeholder == _COMPOSER_PLACEHOLDER
            assert composer.value == "尚未发送的草稿"
            assert len(app._notifications) == 0
            app._last_ctrl_c -= 3
            app._expire_ctrl_c_confirmation()
            assert app._last_ctrl_c == 0
            assert app._current_status == "就绪"
            assert composer.border_title is None
            assert composer.value == "尚未发送的草稿"

            worker = _RunningWorker()
            app._active_worker = worker  # type: ignore[assignment]
            app.action_interrupt_or_exit()
            await pilot.pause()
            assert worker.cancelled is True
            assert app._last_ctrl_c == 0
            assert app._current_status == "正在取消"
            assert str(composer.border_title) == "正在取消当前回答…"
            assert composer.placeholder == _COMPOSER_PLACEHOLDER
            assert len(app._notifications) == 0

    asyncio.run(check())


def test_continued_session_restores_latest_observer_progress() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=_external,
            initial_observer_status={
                "run_id": "previous-run",
                "status": "finalized",
                "progress_markdown": "✓ 上一轮任务已完成\n\n- 已整理项目结构",
            },
        )
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            assert "已恢复最近 Turn" in str(
                app.query_one("#observer-caption", Static).render(),
            )
            progress = app.query_one("#observer-progress", Markdown)
            assert "上一轮任务已完成" in str(progress._markdown)
            assert "已整理项目结构" in str(progress._markdown)

    asyncio.run(check())


def test_slash_menu_supports_keyboard_tab_and_click_selection() -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=_external,
        )
        async with app.run_test(size=(120, 34)) as pilot:
            composer = app.query_one("#composer", Input)
            menu = app.query_one("#command-menu", OptionList)
            await pilot.click("#composer")
            await pilot.press("/")
            await pilot.pause()
            assert menu.display is True
            assert menu.option_count > 3
            assert menu.region.y < composer.region.y
            await pilot.press("down", "tab")
            await pilot.pause()
            assert composer.value == "/code"
            assert menu.display is False

            composer.value = "/cron"
            await pilot.pause()
            assert menu.display is True
            await pilot.click("#command-menu", offset=(8, 1))
            await pilot.pause()
            assert composer.value == "/cron status"
            assert menu.display is False

    asyncio.run(check())


@pytest.mark.parametrize(
    ("reported", "expected"),
    [(True, "缓存 75.0%"), (False, "缓存 ??.?%")],
)
def test_prefix_cache_status_is_kept_in_the_bottom_right_footer(
    reported: bool,
    expected: str,
) -> None:
    async def check() -> None:
        app = YuanYeChatApp(
            _CacheUsageClient(reported=reported),
            "project",
            session_id="session",
            records=[],
            external_command=_external,
        )
        async with app.run_test(size=(140, 34)) as pilot:
            worker = app.run_agent("hello")
            await worker.wait()
            await pilot.pause()
            cache_status = app.query_one("#cache-status", Static)
            assert expected in str(cache_status.render())
            assert cache_status.region.x > app.size.width // 2
            assert cache_status.region.y > app.query_one("#timeline").region.y

    asyncio.run(check())


def test_prefix_cache_status_restores_from_latest_session_record() -> None:
    async def check() -> None:
        records = [{
            "role": "assistant",
            "content": "done",
            "model": {"provider": "deepseek", "name": "deepseek-chat"},
            "model_calls": [{
                "prefix_cache": {
                    "status": "reported",
                    "hit_tokens": 40,
                    "total_tokens": 50,
                },
            }],
        }]
        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=records,
            external_command=_external,
        )
        async with app.run_test(size=(140, 34)) as pilot:
            await pilot.pause()
            assert "缓存 80.0%" in str(
                app.query_one("#cache-status", Static).render(),
            )
            assert "deepseek-chat" in str(
                app.query_one("#model-switch", Button).label,
            )

    asyncio.run(check())


def test_model_switcher_opens_downward_and_shift_tab_cycles() -> None:
    async def check() -> None:
        options = (
            SimpleNamespace(
                profile_id="default", provider="deepseek",
                model="deepseek-chat", selected=True,
            ),
            SimpleNamespace(
                profile_id="flash", provider="deepseek",
                model="deepseek-flash", selected=False,
            ),
            SimpleNamespace(
                profile_id="pro", provider="openai",
                model="gpt-pro", selected=False,
            ),
        )
        app = YuanYeChatApp(
            _Client(), "project", session_id="session", records=[],
            external_command=_external, model_options=options,
        )
        async with app.run_test(size=(140, 34)) as pilot:
            switch = app.query_one("#model-switch", Button)
            menu = app.query_one("#model-menu", OptionList)
            assert "deepseek-chat" in str(switch.label)
            assert await pilot.click(switch)
            await pilot.pause()
            assert menu.display is True
            assert menu.region.y >= switch.region.bottom

            # Clicking elsewhere dismisses without changing the selection.
            assert await pilot.click("#observer-heading")
            await pilot.pause()
            assert menu.display is False
            assert app._model_profile_id == "default"

            assert await pilot.click(switch)
            await pilot.pause()
            assert menu.display is True
            assert await pilot.click("#model-menu", offset=(3, 2))
            await pilot.pause()
            assert menu.display is False
            assert app._model_profile_id == "flash"

            # Any input operation closes the transient menu.
            assert await pilot.click(switch)
            await pilot.pause()
            app.query_one("#composer", Input).value = "h"
            await pilot.pause()
            assert menu.display is False

            await pilot.press("shift+tab")
            await pilot.pause()
            assert "gpt-pro" in str(switch.label)
            assert app._model_profile_id == "pro"

    asyncio.run(check())


def test_selected_model_profile_is_sent_with_the_next_run() -> None:
    async def check() -> None:
        client = _SelectedModelClient()
        options = (
            SimpleNamespace(
                profile_id="default", provider="deepseek",
                model="deepseek-chat", selected=True,
            ),
            SimpleNamespace(
                profile_id="flash", provider="deepseek",
                model="deepseek-flash", selected=False,
            ),
        )
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external, model_options=options,
        )
        async with app.run_test(size=(140, 34)) as pilot:
            app.action_cycle_model()
            worker = app.run_agent("hello")
            await worker.wait()
            await pilot.pause()
            assert client.selected_profile == "flash"

    asyncio.run(check())


def test_reasoning_selector_is_anchored_and_preserves_provider_value() -> None:
    async def check() -> None:
        client = _SelectedModelClient()
        options = (
            SimpleNamespace(
                profile_id="default", provider="openai", model="gpt-5.6",
                selected=True, reasoning_effort="high",
            ),
            SimpleNamespace(
                profile_id="fast", provider="openai", model="gpt-5.5",
                selected=False, reasoning_effort="none",
            ),
        )
        app = YuanYeChatApp(
            client, "project", session_id="session", records=[],
            external_command=_external, model_options=options,
        )
        async with app.run_test(size=(140, 34)) as pilot:
            switch = app.query_one("#model-switch", Button)
            selector = app.query_one("#reasoning-switch", Select)
            assert selector.region.x >= switch.region.right
            assert selector.value == "high"

            # Textual owns the mouse/keyboard card and positions its overlay
            # directly below the selected effort in the top bar.
            current = selector.query_one("SelectCurrent")
            assert await pilot.click(current)
            await pilot.pause()
            overlay = selector.query_one("SelectOverlay")
            assert overlay.display is True
            assert overlay.region.y >= selector.region.bottom

            selector.value = "max"
            await pilot.pause()
            assert app._reasoning_effort == "max"

            # Model switching does not reinterpret the provider's effort
            # contract. The selected API receives the exact user value.
            app._select_model(1)
            await pilot.pause()
            assert selector.value == "max"
            worker = app.run_agent("hello")
            await worker.wait()
            assert client.selected_profile == "fast"
            assert client.selected_reasoning_effort == "max"

    asyncio.run(check())


def test_running_turn_keeps_previous_cache_value_until_new_usage_arrives() -> None:
    async def check() -> None:
        client = _WaitingClient()
        records = [{
            "role": "assistant",
            "content": "done",
            "model": {"provider": "deepseek", "name": "deepseek-chat"},
            "model_calls": [{
                "prefix_cache": {
                    "status": "reported",
                    "hit_tokens": 40,
                    "total_tokens": 50,
                },
            }],
        }]
        app = YuanYeChatApp(
            client, "project", session_id="session", records=records,
            external_command=_external,
        )
        async with app.run_test(size=(140, 34)) as pilot:
            worker = app.run_agent("hello")
            await asyncio.wait_for(client.started.wait(), timeout=2)
            await pilot.pause()
            assert "缓存 80.0%" in str(
                app.query_one("#cache-status", Static).render(),
            )
            client.release.set()
            await worker.wait()

    asyncio.run(check())
