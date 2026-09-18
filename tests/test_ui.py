"""Web UI 的本机安全约束测试。"""

import unittest
import asyncio
import io
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
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
    _handle_gateway_skill_command,
    _handle_harness_command,
    _handle_inbox_command,
    _latest_session_observer_status,
    _render,
    _render_inbox_table,
    _render_restored_history,
    _render_skill_catalog,
    app,
)
from run_ui.approval import InteractiveApproval, _arguments_preview
from run_ui.web import create_app
from tool import ToolContext


class UiTests(unittest.TestCase):
    """验证创建应用时不会开放远程监听配置。"""

    def test_app_exposes_random_token(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            app = create_app("test-token", agent_root=Path(value))
            self.assertEqual(app.state.access_token, "test-token")

    def test_sessions_are_ordered_by_latest_durable_activity(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            active = memory.create_session("first")
            newer = memory.create_session("second")
            memory.record_user(active, "older activity")
            memory.record_user(newer, "newer activity")
            os.utime(
                memory.sessions.active_path(active),
                (2_000_000_000, 2_000_000_000),
            )
            self.assertEqual(memory.list_sessions()[0]["session_id"], active)

    def test_restored_history_is_rendered_without_reasoning(self) -> None:
        output = io.StringIO()
        local_console = Console(file=output, force_terminal=False, width=160)
        records = [
            {"role": "user", "content": "此前问题", "timestamp": "2026-08-09 10:00:00"},
            {
                "role": "assistant", "content": "此前回答",
                "timestamp": "2026-08-09 10:00:01", "reasoning": "内部推理不得显示",
            },
        ]
        with patch("run_ui.cli.console", local_console):
            _render_restored_history(records)
        rendered = output.getvalue()
        self.assertIn("已恢复的对话上下文", rendered)
        self.assertIn("此前问题", rendered)
        self.assertIn("此前回答", rendered)
        self.assertNotIn("内部推理不得显示", rendered)

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
                self.assertTrue(fake.items[0]["read"])
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

    def test_tui_tables_preserve_metadata_and_elide_only_prose(self) -> None:
        output = io.StringIO()
        local_console = Console(file=output, force_terminal=False, width=110)
        item_id = "5697e8da292a"
        created_at = "2026-09-18T10:20:30+08:00"
        location = "skills/search-summary-paper/SKILL.md"
        long_prose = "very long descriptive prose " * 12

        with patch("run_ui.cli.console", local_console):
            _render_inbox_table([{
                "item_id": item_id,
                "status": "failed",
                "title": "Start Coding Session",
                "summary": long_prose,
                "created_at": created_at,
                "read": False,
            }], unread_only=True)
            _render_skill_catalog([(
                "search-summary-paper",
                long_prose,
                location,
            )])

        rendered = output.getvalue()
        self.assertIn(item_id, rendered)
        self.assertIn(created_at, rendered)
        self.assertIn("search-summary-paper", rendered)
        self.assertIn(location, "".join(rendered.split()))
        self.assertIn("…", rendered)
        self.assertNotIn(long_prose, rendered)

    def test_tui_confirmation_callback_replaces_terminal_prompts(self) -> None:
        class Client:
            async def manage_skill(self, payload):
                self.skill_payload = payload
                return {"status": "installed", "message": "installed"}

            async def run_harness_dream(self, selected):
                self.harness_selected = selected
                return {"status": "started"}

        async def run_case() -> tuple[list[tuple[str, str]], Client]:
            client = Client()
            confirmations: list[tuple[str, str]] = []

            async def confirm(title: str, message: str) -> bool:
                confirmations.append((title, message))
                return True

            await _handle_gateway_skill_command(
                client, "project", "session",
                "/skill update sample https://example.invalid/skill.git",
                confirm=confirm,
            )
            await _handle_harness_command(
                client, "/harness dream run", confirm=confirm,
            )
            return confirmations, client

        with (
            patch("run_ui.cli.console", Console(file=io.StringIO(), force_terminal=False)),
            patch("run_ui.cli.typer.confirm", side_effect=AssertionError("terminal prompt")),
        ):
            confirmations, client = asyncio.run(run_case())
        self.assertEqual(
            [item[0] for item in confirmations], ["更新 Skill", "Harness Dream"],
        )
        self.assertTrue(client.skill_payload["confirmed"])
        self.assertIsNone(client.harness_selected)

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

                async def acknowledge_session_history(self, project_id, selected_session, records):
                    del project_id, selected_session, records
                    return {"acknowledged": 0}

            runner = CliRunner()
            with patch("run_ui.cli._gateway_client", return_value=FakeGatewayClient()):
                listed = runner.invoke(app, ["session", "list"])
                shown = runner.invoke(app, ["session", "show", session_id])
                missing = runner.invoke(app, ["chat", "--session", "missing-session"])
                continued = runner.invoke(app, ["chat", "--continue"], input="/exit\n")
            self.assertEqual(listed.exit_code, 0)
            self.assertIn(session_id, listed.stdout)
            self.assertEqual(shown.exit_code, 0)
            self.assertIn("第一答", shown.stdout)
            self.assertNotEqual(missing.exit_code, 0)
            self.assertEqual(continued.exit_code, 0)
            self.assertIn(session_id, continued.stdout)

    def test_web_client_handles_compression_events(self) -> None:
        script = (Path(__file__).parents[1] / "run_ui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data.type==="compression_started"', script)
        self.assertIn('data.type==="context_compressed"', script)
        self.assertIn('data.type==="compression_fallback"', script)
        self.assertIn('data.type==="model_retry"', script)
        self.assertIn('data.type==="model_reconnected"', script)

    def test_continue_restores_latest_available_observer_for_session(self) -> None:
        class Run:
            def __init__(self, run_id, session_id, workload_kind="chat"):
                self.run_id = run_id
                self.session_id = session_id
                self.workload_kind = workload_kind

        class Client:
            async def runs(self, project_id):
                self.project_id = project_id
                return [
                    Run("other", "other-session"),
                    Run("newest", "session"),
                    Run("previous", "session"),
                ]

            async def observer_status(self, run_id):
                if run_id == "newest":
                    raise RuntimeError("Observer not created for this Run")
                return {"run_id": run_id, "progress_markdown": "restored progress"}

        async def check():
            client = Client()
            status = await _latest_session_observer_status(
                client, "project", "session",
            )
            self.assertEqual(client.project_id, "project")
            self.assertEqual(status["run_id"], "previous")

        asyncio.run(check())

    def test_gateway_resume_uses_control_plane_client_without_auto_start(self) -> None:
        created: dict[str, object] = {}

        class Client:
            def __init__(self, agent_root, **kwargs):
                created.update(agent_root=agent_root, **kwargs)

            async def resume(self, epoch, revision):
                created.update(epoch=epoch, revision=revision)
                return {"state": "running"}

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as value, \
             patch(
                 "run_ui.cli.load_runtime_config",
                 return_value=SimpleNamespace(
                     agent_root=Path(value),
                     gateway_port=8765,
                 ),
             ), \
             patch("run_ui.cli.GatewayClient", Client):
            result = runner.invoke(
                app,
                ["gateway", "resume", "--epoch", "7", "--revision", "11"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(created["auto_start"], False)
        self.assertEqual(created["epoch"], 7)
        self.assertEqual(created["revision"], 11)

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
        keys = iter(["down", "enter"])
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

    def test_approval_arguments_preview_is_limited_to_five_short_lines(self) -> None:
        preview = _arguments_preview({
            "candidates": [
                {"title": f"Paper {index}", "authors": ["Author A", "Author B"]}
                for index in range(10)
            ],
        })
        lines = preview.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(len(line) <= 88 for line in lines))
        self.assertIn("已隐藏", lines[-1])

    def test_arrow_menu_defaults_to_execute_but_timeout_and_escape_deny(self) -> None:
        output = io.StringIO()
        approval = InteractiveApproval(
            Console(file=output, force_terminal=True, width=100),
            key_reader=lambda: "enter",
        )
        self.assertTrue(asyncio.run(approval("write", {"path": "allowed.txt"})))

        timed_out = InteractiveApproval(
            Console(file=io.StringIO(), force_terminal=True, width=100),
            key_reader=lambda: "timeout",
        )
        self.assertFalse(asyncio.run(timed_out("write", {"path": "timed-out.txt"})))
        self.assertTrue(timed_out.last_timed_out)

        escaped = InteractiveApproval(
            Console(file=io.StringIO(), force_terminal=True, width=100),
            key_reader=lambda: "escape",
        )
        self.assertFalse(asyncio.run(escaped("write", {"path": "cancelled.txt"})))
