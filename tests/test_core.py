"""新核心链路的确定性回归测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from Agent import AgentRuntime, EventType, load_runtime_config
from Agent.contracts import ModelReply, ToolCall
from bootstrap import initialize_project
from memory import MemoryStore
from prompt import PromptComposer
from tools import AsyncToolRegistry, ToolContext, default_tools


class ToolProvider:
    """先请求计算工具、再依据 Observation 完成的测试模型。"""

    async def complete(self, messages, tools):
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(tool_calls=(ToolCall("calculator", {"expression": "2 + 2"}),), finished=False)
        return ModelReply("计算完成：4")


class StreamProvider:
    """逐段输出文本的测试 Provider。"""

    streaming = True

    async def complete(self, messages, tools):
        return ModelReply("不应走完整响应")

    async def stream(self, messages, tools):
        yield ModelReply("你", finished=False)
        yield ModelReply("好", finished=False)
        yield ModelReply(finished=True)


class CoreTests(unittest.TestCase):
    """覆盖配置、Runtime、工具边界与记忆目录。"""

    def test_initializer_creates_complete_yy_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            yy = initialize_project(root)
            local = yy / "settings.local.json"
            self.assertTrue(local.exists())
            self.assertTrue((yy / "memory" / "session" / "index.json").exists())
            for name in ("USER.md", "RESEARCH.md", "OTHERS.md"):
                self.assertTrue((yy / "memory" / "profile" / name).exists())
            local.write_text("用户配置", encoding="utf-8")
            initialize_project(root)
            self.assertEqual(local.read_text(encoding="utf-8"), "用户配置")

    def test_config_uses_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".yy").mkdir()
            (root / ".yy" / "settings.json").write_text('{"model":"a"}', encoding="utf-8")
            (root / ".yy" / "settings.local.json").write_text('{"model":"b","base_url":"https://gateway.example/v1","api_key":"local-key"}', encoding="utf-8")
            config = load_runtime_config(root)
            self.assertEqual(config.model, "b")
            self.assertEqual(config.base_url, "https://gateway.example/v1")
            self.assertEqual(config.api_key, "local-key")
            self.assertFalse(config.stream)

    def test_shared_configuration_rejects_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".yy").mkdir()
            (root / ".yy" / "settings.json").write_text('{"api_key":"must-not-be-here"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "settings.local.json"):
                load_runtime_config(root)

    def test_configuration_requires_boolean_stream(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".yy").mkdir()
            (root / ".yy" / "settings.local.json").write_text('{"stream":"yes"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stream"):
                load_runtime_config(root)

    def test_memory_uses_timestamped_jsonl_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / ".yy" / "memory"
            memory = MemoryStore(root)
            session_id = memory.create_session("你好")
            memory.record_user(session_id, "你好")
            memory.record_assistant(session_id, "你好，我可以帮助你。")
            index = json.loads((root / "session" / "index.json").read_text(encoding="utf-8"))
            filename = index["sessions"][session_id]["latest_file"]
            self.assertRegex(filename, rf"^\d{{4}}-\d{{2}}-\d{{2}}_{session_id}_001\.jsonl$")
            records = [json.loads(line) for line in (root / "session" / filename).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["role"], "user")
            self.assertIn("timestamp", records[0])
            self.assertEqual(memory.restore_messages(session_id)[1]["content"], "你好，我可以帮助你。")

    def test_new_session_segment_keeps_hash_and_updates_index(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            session_id = memory.create_session("第一句话")
            path = memory.sessions.start_new_segment(session_id)
            self.assertEqual(path.name.split("_")[1], session_id)
            self.assertTrue(path.name.endswith("_002.jsonl"))

    def test_prompt_restores_latest_session_messages(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            memory = MemoryStore(root / ".yy" / "memory")
            session_id = memory.create_session("第一句")
            memory.record_user(session_id, "第一句")
            memory.record_assistant(session_id, "第一答")
            messages = PromptComposer(root, memory).compose("第二句", session_id)
            self.assertEqual([(item["role"], item["content"]) for item in messages[1:]], [("user", "第一句"), ("assistant", "第一答"), ("user", "第二句")])

    def test_memory_initialization_creates_extensible_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / ".yy" / "memory"
            memory = MemoryStore(root)
            for name in ("USER.md", "RESEARCH.md", "OTHERS.md"):
                self.assertTrue((root / "profile" / name).exists())
            extra = root / "profile" / "PROJECT.md"
            extra.write_text("Agent 项目", encoding="utf-8")
            self.assertIn("PROJECT", memory.profile_context())

    def test_runtime_runs_async_react_loop(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            runtime = AgentRuntime(load_runtime_config(Path(value)), provider=ToolProvider())
            result = asyncio.run(runtime.run("计算 2 + 2"))
            self.assertTrue(result.completed)
            self.assertEqual(result.answer, "计算完成：4")

    def test_runtime_emits_streaming_text_events_in_order(self) -> None:
        async def collect() -> list[str]:
            with tempfile.TemporaryDirectory() as value:
                runtime = AgentRuntime(load_runtime_config(Path(value)), provider=StreamProvider())
                return [str(event.payload["content"]) async for event in runtime.run_turn("问候") if event.type is EventType.TEXT]
        self.assertEqual(asyncio.run(collect()), ["你", "好"])

    def test_write_tool_requires_approval_and_stays_in_workspace(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                context = ToolContext(Path(value))
                tools = default_tools(Path(value))
                with self.assertRaises(PermissionError):
                    await tools.execute("write_file", {"path": "note.txt", "content": "x"}, context)
                with self.assertRaises(PermissionError):
                    await tools.execute("read_file", {"path": "../secret.txt"}, context)
        asyncio.run(check())
