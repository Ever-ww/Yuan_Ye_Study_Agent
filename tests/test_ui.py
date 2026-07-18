"""Web UI 的本机安全约束测试。"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner
from memory import MemoryStore
from run_ui.cli import app
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
