from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from Agent.config import RuntimeConfig
from tool.contracts import ToolContext
from tool.defaults import default_tools
from tool.registry import AsyncToolRegistry
from tools.browser_use import BrowserUseTool


class _FakePage:
    async def get_element(self, backend_node_id: int):
        assert backend_node_id == 42
        return _FakeElement()

    async def get_url(self):
        return "https://example.com/"

    async def evaluate(self, script: str):
        return "page text" if "innerText" in script else "ok"


class _FakeBrowser:
    def __init__(self):
        self.page = _FakePage()
        self.started = False
        self.visited: list[str] = []

    async def start(self):
        self.started = True

    async def get_current_page(self):
        return self.page

    async def get_state_as_text(self):
        return "interactive state"

    async def navigate_to(self, url: str, new_tab: bool = False):
        self.visited.append(url)

    async def get_element_by_index(self, index: int):
        return type("Node", (), {"backend_node_id": 42})() if index == 1 else None

    async def kill(self):
        self.started = False


class _FakeElement:
    async def click(self):
        return None

    async def fill(self, value: str):
        assert value == "query"


class BrowserUseToolTests(unittest.TestCase):
    def test_policy_and_risk(self):
        tool = BrowserUseTool(Path.cwd())
        self.assertEqual(tool.risk_for({"action": "state"}), "read")
        self.assertEqual(tool.risk_for({"action": "click", "index": 1}), "high")
        self.assertEqual(tool.risk_for({"action": "open", "url": "https://example.com"}), "high")
        self.assertEqual(tool._validate_url("https://example.com"), "https://example.com")
        with self.assertRaises(ValueError):
            tool._validate_url("http://127.0.0.1:8765")
        with self.assertRaises(ValueError):
            tool._validate_url("https://user:secret@example.com/")
        context = ToolContext(project_root=Path.cwd())
        registry = AsyncToolRegistry([tool])
        with self.assertRaises(ValueError):
            registry.prepare_invocation(
                "browser_use", {"action": "open", "url": "http://127.0.0.1:8765"},
                context, tool_call_id="blocked",
            )
        prepared = registry.prepare_invocation(
            "browser_use", {"action": "open", "url": "https://example.com"},
            context, tool_call_id="approved",
        )
        self.assertTrue(prepared.approval_required)

    def test_template_configuration_and_main_agent_only(self):
        template = json.loads(
            Path("bootstrap/templates/settings.local.json.example").read_text(encoding="utf-8"),
        )
        config = RuntimeConfig.model_validate({
            **template,
            "agent_root": Path.cwd(),
            "workspace_root": Path.cwd(),
        })
        self.assertEqual(config.browser_use_timeout_seconds, 60.0)
        interactive = default_tools(Path.cwd(), browser_use_enabled=True)
        harness = default_tools(Path.cwd(), runtime_profile="harness", browser_use_enabled=True)
        self.assertIn("browser_use", interactive.names())
        self.assertNotIn("browser_use", harness.names())

    def test_browser_profile_is_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            first = BrowserUseTool(root / "first", agent_root=root / "agent")
            second = BrowserUseTool(root / "second", agent_root=root / "agent")
            self.assertNotEqual(first.workspace_root, second.workspace_root)
            self.assertEqual(first.agent_root, second.agent_root)

    def test_local_browser_actions_are_serialized_and_session_is_reused(self):
        async def check():
            with tempfile.TemporaryDirectory() as value:
                browser = _FakeBrowser()
                async def factory():
                    return browser
                tool = BrowserUseTool(Path(value), session_factory=factory)
                context = ToolContext(project_root=Path(value))
                first = await tool.run({"action": "state"}, context)
                second = await tool.run({"action": "extract"}, context)
                third = await tool.run({"action": "open", "url": "https://example.com"}, context)
                fourth = await tool.run({"action": "click", "index": 1}, context)
                self.assertIn("interactive state", first)
                self.assertIn("page text", second)
                self.assertIn("opened", third)
                self.assertIn("completed", fourth)
                self.assertEqual(browser.visited, ["https://example.com"])
                self.assertTrue(browser.started)
                await tool.close()
                self.assertFalse(browser.started)
        asyncio.run(check())
