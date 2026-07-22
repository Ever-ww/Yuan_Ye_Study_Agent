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
from run_ui.cli import _active_live, _approve, _render, app
from run_ui.approval import InteractiveApproval
from run_ui.web import create_app


class UiTests(unittest.TestCase):
    """验证创建应用时不会开放远程监听配置。"""

    def test_app_exposes_random_token(self) -> None:
        app = create_app("test-token")
        self.assertEqual(app.state.access_token, "test-token")

    def test_session_commands_list_and_show_restorable_history(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            session_id = memory.create_session("第一句")
            memory.record_user(session_id, "第一句")
            memory.record_assistant(session_id, "第一答")
            runner = CliRunner()
            with patch("run_ui.cli._memory", return_value=memory):
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
                    result = await _approve("write_file", {"path": "demo.txt", "content": "测试"})
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
                    result = await _approve("write_file", {"path": "demo.txt", "content": "测试"})
                return result, live.calls
            finally:
                _active_live.reset(token)

        result, calls = asyncio.run(cancel())
        self.assertFalse(result)
        self.assertEqual(calls, ["stop", ("start", True)])

    def test_write_tool_approval_completes_inside_real_live_render(self) -> None:
        class WriteProvider:
            streaming = False

            async def complete(self, messages, tools):
                if not any(message.get("role") == "tool" for message in messages):
                    return ModelReply(tool_calls=(ToolCall(
                        name="write_file",
                        arguments={"path": "approval-test.txt", "content": "审批成功"},
                    ),))
                return ModelReply(text="文件已写入")

        async def run_case(root: Path) -> None:
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=WriteProvider(),
                approval=_approve,
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
            first = await approval("write_file", {"path": "first.txt"})
            second = await approval("write_file", {"path": "second.txt"})
            return first, second

        self.assertEqual(asyncio.run(approve_twice()), (True, True))
        self.assertEqual(approval.session_allowed_tools, {"write_file"})

    def test_arrow_menu_defaults_to_deny_and_escape_cancels(self) -> None:
        output = io.StringIO()
        approval = InteractiveApproval(
            Console(file=output, force_terminal=True, width=100),
            key_reader=lambda: "enter",
        )
        self.assertFalse(asyncio.run(approval("write_file", {"path": "denied.txt"})))

        escaped = InteractiveApproval(
            Console(file=io.StringIO(), force_terminal=True, width=100),
            key_reader=lambda: "escape",
        )
        self.assertFalse(asyncio.run(escaped("write_file", {"path": "cancelled.txt"})))
