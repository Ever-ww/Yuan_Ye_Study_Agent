"""Web UI 的本机安全约束测试。"""

import unittest
import asyncio
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import typer
from rich.console import Console
from typer.testing import CliRunner

from Agent import AgentRuntime, load_runtime_config
from Agent.contracts import ModelReply, ToolCall
from memory import MemoryStore
from run_ui.cli import (
    ChatInterruptController,
    _active_live,
    _approve,
    _handle_inbox_command,
    _render,
    app,
)
from run_ui.approval import InteractiveApproval
from run_ui.web import create_app
from tool import ToolContext


class UiTests(unittest.TestCase):
    """验证创建应用时不会开放远程监听配置。"""

    def test_app_exposes_random_token(self) -> None:
        app = create_app("test-token")
        self.assertEqual(app.state.access_token, "test-token")

    def test_cli_inbox_lists_shows_and_marks_background_results(self) -> None:
        class FakeGatewayClient:
            def __init__(self) -> None:
                self.items = [
                    {
                        "item_id": "abcdef1234567890",
                        "run_id": "run-1",
                        "project_id": "project-1",
                        "session_id": "session-1",
                        "title": "未读任务",
                        "summary": "后台执行成功",
                        "status": "completed",
                        "created_at": "2026-08-02 10:00:00",
                        "read": False,
                    },
                    {
                        "item_id": "fedcba9876543210",
                        "run_id": "run-2",
                        "project_id": "project-1",
                        "session_id": None,
                        "title": "已读任务",
                        "summary": "历史结果",
                        "status": "failed",
                        "created_at": "2026-08-01 10:00:00",
                        "read": True,
                    },
                ]

            async def inbox(self, unread_only=False):
                return [dict(item) for item in self.items if not unread_only or not item["read"]]

            async def mark_inbox_read(self, item_id):
                item = next(item for item in self.items if item["item_id"] == item_id)
                item["read"] = True
                return dict(item)

        async def check() -> str:
            fake = FakeGatewayClient()
            output = io.StringIO()
            local_console = Console(file=output, force_terminal=False, width=180)
            with patch("run_ui.cli.console", local_console):
                await _handle_inbox_command(fake, "/inbox")
                first = output.getvalue()
                self.assertIn("未读任务", first)
                self.assertNotIn("已读任务", first)
                await _handle_inbox_command(fake, "/inbox all")
                await _handle_inbox_command(fake, "/inbox show abcdef123456")
                await _handle_inbox_command(fake, "/inbox read abcdef123456")
                self.assertTrue(fake.items[0]["read"])
                fake.items[0]["read"] = False
                await _handle_inbox_command(fake, "/inbox read-all")
                self.assertTrue(all(item["read"] for item in fake.items))
            return output.getvalue()

        rendered = asyncio.run(check())
        self.assertIn("已读任务", rendered)
        self.assertIn("后台执行成功", rendered)
        self.assertIn("已标记为已读", rendered)
        self.assertIn("已将 1 条 Inbox 结果标记为已读", rendered)

    def test_session_commands_list_and_show_restorable_history(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            session_id = memory.create_session("第一句")
            memory.record_user(session_id, "第一句")
            memory.record_assistant(session_id, "第一答")

            class FakeGatewayClient:
                async def register_project(self, path):
                    del path
                    return {"project_id": "project"}

                async def sessions(self, project_id):
                    del project_id
                    return memory.list_sessions()

                async def session(self, project_id, selected_session):
                    del project_id
                    return memory.session_records(selected_session)

                async def inbox(self, unread_only=False):
                    del unread_only
                    return []

            runner = CliRunner()
            with patch("run_ui.cli._gateway_client", return_value=FakeGatewayClient()):
                listed = runner.invoke(app, ["session", "list"])
                shown = runner.invoke(app, ["session", "show", session_id])
                missing = runner.invoke(app, ["chat", "--session", "missing-session"])
            self.assertEqual(listed.exit_code, 0)
            self.assertIn(session_id, listed.stdout)
            self.assertEqual(shown.exit_code, 0)
            self.assertIn("第一答", shown.stdout)
            self.assertNotEqual(missing.exit_code, 0)

    def test_web_client_handles_compression_events(self) -> None:
        script = (Path(__file__).parents[1] / "run_ui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data.type==="compression_started"', script)
        self.assertIn('data.type==="context_compressed"', script)
        self.assertIn('data.type==="compression_fallback"', script)
        self.assertIn('data.type==="model_retry"', script)
        self.assertIn('data.type==="model_reconnected"', script)

    def test_ctrl_c_cancels_active_answer_and_is_exit_when_idle(self) -> None:
        async def check() -> None:
            controller = ChatInterruptController()
            controller.bind(asyncio.get_running_loop())
            active = asyncio.create_task(asyncio.Event().wait())
            controller.set_active(active)
            controller.handle_sigint(2, None)
            with self.assertRaises(asyncio.CancelledError):
                await active
            self.assertTrue(controller.consume_cancel_request())
            controller.clear_active()
            with self.assertRaises(KeyboardInterrupt):
                controller.handle_sigint(2, None)

        asyncio.run(check())

    def test_tool_approval_pauses_and_resumes_live_display(self) -> None:
        class FakeLive:
            def __init__(self) -> None:
                self.calls: list[object] = []

            def stop(self) -> None:
                self.calls.append("stop")

            def start(self, *, refresh: bool = False) -> None:
                self.calls.append(("start", refresh))

        async def approve() -> tuple[bool, list[object]]:
            live = FakeLive()
            token = _active_live.set(live)
            try:
                with patch("run_ui.cli.typer.confirm", return_value=True):
                    result = await _approve("write", {"path": "demo.txt", "content": "测试"})
                return result, live.calls
            finally:
                _active_live.reset(token)

        result, calls = asyncio.run(approve())
        self.assertTrue(result)
        self.assertEqual(calls, ["stop", ("start", True)])

    def test_cancelled_tool_approval_is_a_normal_rejection(self) -> None:
        class FakeLive:
            def __init__(self) -> None:
                self.calls: list[object] = []

            def stop(self) -> None:
                self.calls.append("stop")

            def start(self, *, refresh: bool = False) -> None:
                self.calls.append(("start", refresh))

        async def cancel() -> tuple[bool, list[object]]:
            live = FakeLive()
            token = _active_live.set(live)
            try:
                with patch("run_ui.cli.typer.confirm", side_effect=typer.Abort()):
                    result = await _approve("write", {"path": "demo.txt", "content": "测试"})
                return result, live.calls
            finally:
                _active_live.reset(token)

        result, calls = asyncio.run(cancel())
        self.assertFalse(result)
        self.assertEqual(calls, ["stop", ("start", True)])

    def test_write_tool_approval_completes_inside_real_live_render(self) -> None:
        class CheckpointSandbox:
            async def checkpoint_write(self, path: str):
                del path
                return type("Checkpoint", (), {"commit_sha": "0" * 40})()

            async def restore_current(self):
                return None

        class WriteProvider:
            streaming = False

            async def complete(self, messages, tools):
                if not any(message.get("role") == "tool" for message in messages):
                    return ModelReply(tool_calls=(ToolCall(
                        name="write",
                        arguments={"path": "approval-test.txt", "content": "审批成功"},
                    ),))
                return ModelReply(text="文件已写入")

        async def run_case(root: Path) -> None:
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=WriteProvider(),
                tool_context=ToolContext(
                    project_root=root,
                    approval=_approve,
                    sandbox=CheckpointSandbox(),
                ),
                enable_sandbox=False,
            )
            try:
                with patch("run_ui.cli.typer.confirm", return_value=True):
                    await _render(runtime, "写入测试", propagate_errors=True)
            finally:
                await runtime.close()

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            asyncio.run(run_case(root))
            self.assertEqual((root / "approval-test.txt").read_text(encoding="utf-8"), "审批成功")

    def test_arrow_menu_can_allow_tool_for_current_session(self) -> None:
        keys = iter(["up", "enter"])
        output = io.StringIO()
        approval = InteractiveApproval(
            Console(file=output, force_terminal=True, width=100),
            key_reader=lambda: next(keys),
        )

        async def approve_twice() -> tuple[bool, bool]:
            first = await approval("write", {"path": "first.txt"})
            second = await approval("write", {"path": "second.txt"})
            return first, second

        self.assertEqual(asyncio.run(approve_twice()), (True, True))
        self.assertEqual(approval.session_allowed_tools, {"write"})

    def test_arrow_menu_defaults_to_deny_and_escape_cancels(self) -> None:
        output = io.StringIO()
        approval = InteractiveApproval(
            Console(file=output, force_terminal=True, width=100),
            key_reader=lambda: "enter",
        )
        self.assertFalse(asyncio.run(approval("write", {"path": "denied.txt"})))

        escaped = InteractiveApproval(
            Console(file=io.StringIO(), force_terminal=True, width=100),
            key_reader=lambda: "escape",
        )
        self.assertFalse(asyncio.run(escaped("write", {"path": "cancelled.txt"})))
