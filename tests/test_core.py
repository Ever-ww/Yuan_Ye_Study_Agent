"""新核心链路的确定性回归测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from Agent import AgentRuntime, EventType, HookEvent, HookPoint, HookRegistry, load_runtime_config
from Agent.contracts import ModelReply, TokenUsage, ToolCall
from bootstrap import ensure_project_initialized, is_project_initialized
from memory import MemoryStore
from prompt import PromptComposer
from tools import AsyncToolRegistry, ToolContext, default_tools


class ToolProvider:
    """先请求计算工具、再依据 Observation 完成的测试模型。"""

    async def complete(self, messages, tools):
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(tool_calls=(ToolCall("calculator", {"expression": "2 + 2"}),), finished=False)
        return ModelReply("计算完成：4")


class MultiToolProvider:
    """一个模型 Turn 同时请求两个工具。"""

    streaming = False

    async def complete(self, messages, tools):
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(tool_calls=(
                ToolCall("calculator", {"expression": "10 + 20"}, "call_a"),
                ToolCall("calculator", {"expression": "30 * 2"}, "call_b"),
            ))
        return ModelReply("结果为 60")


class StreamProvider:
    """逐段输出文本的测试 Provider。"""

    streaming = True

    async def complete(self, messages, tools):
        return ModelReply("不应走完整响应")

    async def stream(self, messages, tools):
        yield ModelReply("你", finished=False)
        yield ModelReply("好", finished=False)
        yield ModelReply(finished=True)


class UsageProvider:
    """返回供应商精确 usage 的指标测试 Provider。"""

    streaming = False

    async def complete(self, messages, tools):
        return ModelReply("指标已记录", usage=TokenUsage(input_tokens=128, output_tokens=9))


class CapturingProvider:
    """保存最终发送消息，用于验证记忆由 Hook 注入。"""

    streaming = False

    def __init__(self) -> None:
        self.messages = []

    async def complete(self, messages, tools):
        self.messages = [dict(message) for message in messages]
        return ModelReply("第二答")


class CoreTests(unittest.TestCase):
    """覆盖配置、Runtime、工具边界与记忆目录。"""

    def test_initializer_creates_complete_yy_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            first = ensure_project_initialized(root)
            yy = first.yy_dir
            self.assertTrue(first.initialized)
            self.assertTrue(is_project_initialized(root))
            local = yy / "settings.local.json"
            self.assertTrue(local.exists())
            self.assertTrue((yy / "memory" / "session" / "index.json").exists())
            for name in ("USER.md", "RESEARCH.md", "OTHERS.md"):
                self.assertTrue((yy / "memory" / "profile" / name).exists())
            local.write_text("用户配置", encoding="utf-8")
            second = ensure_project_initialized(root)
            self.assertFalse(second.initialized)
            self.assertEqual(local.read_text(encoding="utf-8"), "用户配置")
            (yy / "memory" / "profile" / "OTHERS.md").unlink()
            repaired = ensure_project_initialized(root)
            self.assertTrue(repaired.initialized)
            self.assertTrue((yy / "memory" / "profile" / "OTHERS.md").exists())

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

    def test_memory_callbacks_restore_session_messages(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            memory = MemoryStore(root / ".yy" / "memory")
            session_id = memory.create_session("第一句")
            memory.record_user(session_id, "第一句")
            memory.record_assistant(session_id, "第一答")
            provider = CapturingProvider()
            runtime = AgentRuntime(load_runtime_config(root), provider=provider, memory=memory)
            asyncio.run(runtime.run("第二句", session_id))
            self.assertEqual(
                [(item["role"], item["content"]) for item in provider.messages[1:]],
                [("user", "第一句"), ("assistant", "第一答"), ("user", "第二句")],
            )
            self.assertEqual(PromptComposer(root).compose("纯基础")[1]["content"], "纯基础")

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
            config = load_runtime_config(Path(value))
            memory = MemoryStore(config.memory_dir)
            runtime = AgentRuntime(config, provider=ToolProvider(), memory=memory)
            result = asyncio.run(runtime.run("计算 2 + 2"))
            self.assertTrue(result.completed)
            self.assertEqual(result.answer, "计算完成：4")
            assistant = memory.session_records(result.session_id)[-1]
            self.assertEqual(len(assistant["model_calls"]), 2)
            self.assertTrue(all("turn" not in call for call in assistant["model_calls"]))
            self.assertTrue(all(call["output_tokens_source"] == "estimated" for call in assistant["model_calls"]))

    def test_runtime_records_model_latency_tokens_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, provider="deepseek", model="deepseek-chat", base_url="https://api.deepseek.com/v1")
            memory = MemoryStore(config.memory_dir)
            runtime = AgentRuntime(config, provider=UsageProvider(), memory=memory)
            result = asyncio.run(runtime.run("请记录指标"))
            assistant = memory.session_records(result.session_id)[-1]
            self.assertEqual(assistant["model"]["provider"], "deepseek")
            self.assertEqual(assistant["model"]["name"], "deepseek-chat")
            self.assertGreaterEqual(assistant["task_latency_ms"], 0)
            call = assistant["model_calls"][0]
            self.assertEqual(call["input_tokens"]["context_total"], 128)
            self.assertGreater(call["input_tokens"]["current_question"], 0)
            self.assertEqual(call["input_tokens"]["context_source"], "provider")
            self.assertEqual(call["output_tokens"], 9)
            self.assertEqual(call["output_tokens_source"], "provider")
            self.assertGreaterEqual(call["latency_ms"], 0)

    def test_all_ten_hook_points_follow_turn_and_tool_order(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            points: list[str] = []
            hooks = HookRegistry()

            async def observe(event: HookEvent) -> None:
                points.append(event.point.value)

            for point in HookPoint:
                hooks.register(point, observe)
            runtime = AgentRuntime(load_runtime_config(Path(value)), provider=ToolProvider(), hooks=hooks)
            result = asyncio.run(runtime.run("计算 2 + 2"))
            self.assertTrue(result.completed)
            self.assertEqual(points, [
                "trace_start",
                "turn_start", "model_before", "model_during", "model_after",
                "tool_before", "tool_during", "tool_after", "turn_end",
                "turn_start", "model_before", "model_during", "model_after", "turn_end",
                "trace_end",
            ])

    def test_one_turn_can_execute_multiple_tools(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            tool_events: list[HookEvent] = []
            hooks = HookRegistry()

            async def observe(event: HookEvent) -> None:
                tool_events.append(event)

            hooks.register(HookPoint.TOOL_BEFORE, observe)
            runtime = AgentRuntime(load_runtime_config(Path(value)), provider=MultiToolProvider(), hooks=hooks)
            result = asyncio.run(runtime.run("连续计算"))
            self.assertEqual(result.answer, "结果为 60")
            self.assertEqual(len(tool_events), 2)
            self.assertTrue(all(not hasattr(event, "turn") for event in tool_events))

    def test_tool_hook_rewrite_is_validated_again(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            hooks = HookRegistry()

            async def invalidate(event: HookEvent) -> None:
                event.data["arguments"] = {"expression": 4}

            hooks.register(HookPoint.TOOL_BEFORE, invalidate)
            runtime = AgentRuntime(load_runtime_config(Path(value)), provider=ToolProvider(), hooks=hooks)
            result = asyncio.run(runtime.run("计算"))
            self.assertFalse(result.completed)

    def test_session_hooks_do_not_expose_turn_numbers(self) -> None:
        async def check() -> list[HookEvent]:
            with tempfile.TemporaryDirectory() as value:
                events: list[HookEvent] = []
                hooks = HookRegistry()

                async def observe(event: HookEvent) -> None:
                    events.append(event)

                hooks.register(HookPoint.MODEL_AFTER, observe)
                runtime = AgentRuntime(load_runtime_config(Path(value)), provider=UsageProvider(), hooks=hooks)
                await runtime.run("问题")
                return events

        events = asyncio.run(check())
        self.assertEqual(len(events), 1)
        self.assertFalse(hasattr(events[0], "turn"))
        self.assertNotIn("turn", events[0].data["model_call"])

    def test_runtime_emits_streaming_text_events_in_order(self) -> None:
        async def collect() -> list[str]:
            with tempfile.TemporaryDirectory() as value:
                runtime = AgentRuntime(load_runtime_config(Path(value)), provider=StreamProvider())
                return [str(event.payload["content"]) async for event in runtime.run_task("问候") if event.type is EventType.TEXT]
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
