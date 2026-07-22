"""新核心链路的确定性回归测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import BaseModel, ValidationError

from Agent import AgentRuntime, EventType, HookEvent, HookPoint, HookRegistry, load_runtime_config
from Agent.contracts import ModelReply, TokenUsage, ToolCall
from bootstrap import ensure_project_initialized, is_project_initialized
from context_process import ContextProcessor
from memory import MemoryStore
from prompt import PromptComposer
from tools import AsyncToolRegistry, ToolContext, default_tools


class ToolProvider:
    """先请求计算工具、再依据 Observation 完成的测试模型。"""

    async def complete(self, messages, tools):
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(tool_calls=(ToolCall(name="calculator", arguments={"expression": "2 + 2"}),), finished=False)
        return ModelReply(text="计算完成：4")


class MultiToolProvider:
    """一个模型 Turn 同时请求两个工具。"""

    streaming = False

    async def complete(self, messages, tools):
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(tool_calls=(
                ToolCall(name="calculator", arguments={"expression": "10 + 20"}, id="call_a"),
                ToolCall(name="calculator", arguments={"expression": "30 * 2"}, id="call_b"),
            ))
        return ModelReply(text="结果为 60")


class FailingToolProvider:
    """请求一个必然失败的文件读取工具。"""

    streaming = False

    async def complete(self, messages, tools):
        return ModelReply(tool_calls=(ToolCall(name="read_file", arguments={"path": "missing-file.txt"}),))


class SubagentCallingProvider:
    """请求 subagent，并在收到父级 tool 反馈后完成。"""

    streaming = False

    async def complete(self, messages, tools):
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(tool_calls=(ToolCall(name="subagent", arguments={
                "task": "提炼结论", "instructions": "保持简洁",
            }),))
        return ModelReply(text=f"父 Agent 收到：{messages[-1]['content']}")


class StreamProvider:
    """逐段输出文本的测试 Provider。"""

    streaming = True

    async def complete(self, messages, tools):
        return ModelReply(text="不应走完整响应")

    async def stream(self, messages, tools):
        yield ModelReply(text="你", finished=False)
        yield ModelReply(text="好", finished=False)
        yield ModelReply(finished=True)


class UsageProvider:
    """返回供应商精确 usage 的指标测试 Provider。"""

    streaming = False

    async def complete(self, messages, tools):
        return ModelReply(text="指标已记录", usage=TokenUsage(input_tokens=128, output_tokens=9))


class CompressionProvider:
    """返回可校验双摘要 JSON 的压缩测试模型。"""

    streaming = False

    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.calls = 0
        self.messages = []

    async def complete(self, messages, tools):
        self.calls += 1
        self.messages = [dict(message) for message in messages]
        if not self.valid:
            return ModelReply(text="不是 JSON")
        return ModelReply(text=json.dumps({
            "profile_markdown": "# 用户特征\n- 偏好中文\n\n# 研究方向\n- Agent",
            "context_summary_markdown": "# 用户目标\n研究 Agent\n# 已完成任务\n完成存储\n# 未完成任务\n继续压缩\n# 关键决策\n使用 JSONL\n# 必要工具结论\n计算结果为 4",
        }, ensure_ascii=False))


class LargeUsageProvider:
    """用精确 usage 触发自动压缩。"""

    streaming = False

    async def complete(self, messages, tools):
        return ModelReply(text="已完成大上下文回答", usage=TokenUsage(input_tokens=20000, output_tokens=20))


class CapturingProvider:
    """保存最终发送消息，用于验证记忆由 Hook 注入。"""

    streaming = False

    def __init__(self) -> None:
        self.messages = []

    async def complete(self, messages, tools):
        self.messages = [dict(message) for message in messages]
        return ModelReply(text="第二答")


class CoreTests(unittest.TestCase):
    """覆盖配置、Runtime、工具边界与记忆目录。"""

    def test_core_contracts_are_frozen_pydantic_models(self) -> None:
        """核心数据契约统一由 Pydantic 定义并保持不可变语义。"""
        reply = ModelReply(text="完成")
        self.assertIsInstance(reply, BaseModel)
        with self.assertRaises(ValidationError):
            reply.text = "被修改"

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
            self.assertTrue((yy / "memory" / "profile" / "index.json").exists())
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

    def test_configuration_rejects_unknown_fields(self) -> None:
        """配置拼写错误必须尽早失败，不能被静默忽略。"""
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".yy").mkdir()
            (root / ".yy" / "settings.local.json").write_text('{"streem":false}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "streem"):
                load_runtime_config(root)

    def test_compression_threshold_defaults_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            self.assertEqual(load_runtime_config(root).compression_threshold_tokens, 20000)
            (root / ".yy" / "settings.local.json").write_text(
                '{"compression_threshold_tokens":-1}', encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "compression_threshold_tokens"):
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

    def test_memory_rejects_invalid_jsonl_records(self) -> None:
        """损坏或角色字段非法的持久化记录必须在恢复边界明确失败。"""
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            session_id = memory.create_session("问题")
            active = memory.sessions.directory / memory.active_filename(session_id)
            active.write_text(
                '{"role":"invalid","content":"坏记录","timestamp":"2026-07-23 10:00:00"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "格式无效"):
                memory.session_records(session_id)

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

    def test_tool_calls_and_results_are_persisted_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            result = asyncio.run(AgentRuntime(config, provider=ToolProvider(), memory=memory).run("计算 2 + 2"))
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant", "tool", "assistant"])
            call = records[1]["tool_calls"][0]
            self.assertEqual(call["function"]["name"], "calculator")
            self.assertTrue(call["id"].startswith("call_"))
            self.assertEqual(records[2]["tool_call_id"], call["id"])
            self.assertEqual(records[2]["status"], "success")
            restored = memory.restore_messages(result.session_id)
            self.assertEqual(restored[1]["tool_calls"][0]["id"], restored[2]["tool_call_id"])

    def test_failed_tool_result_is_persisted_before_error_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            result = asyncio.run(AgentRuntime(config, provider=FailingToolProvider(), memory=memory).run("读取缺失文件"))
            self.assertFalse(result.completed)
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant", "tool"])
            self.assertEqual(records[-1]["status"], "error")
            self.assertIn("工具执行失败", records[-1]["content"])

    def test_automatic_compression_merges_profile_and_rolls_over(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, compression_threshold_tokens=20000)
            memory = MemoryStore(config.memory_dir)
            compressor = CompressionProvider()
            runtime = AgentRuntime(
                config,
                provider=LargeUsageProvider(),
                memory=memory,
                compression_provider_factory=lambda: compressor,
            )
            result = asyncio.run(runtime.run("整理长上下文"))
            self.assertTrue(result.completed)
            self.assertEqual(compressor.calls, 1)
            self.assertTrue(memory.active_filename(result.session_id).endswith("_002.jsonl"))
            summary = memory.session_records(result.session_id)[0]
            self.assertEqual(summary["role"], "summary")
            self.assertEqual(memory.restore_messages(result.session_id)[0]["role"], "system")
            profile = config.memory_dir / "profile" / f"{result.session_id}.md"
            self.assertIn("偏好中文", profile.read_text(encoding="utf-8"))
            index = json.loads((config.memory_dir / "profile" / "index.json").read_text(encoding="utf-8"))
            metadata = index["profiles"][result.session_id]
            self.assertEqual(metadata["segments_processed"], 1)
            self.assertEqual(metadata["conversation_turns"], 1)
            self.assertEqual(metadata["records_processed"], 2)
            memory.record_user(result.session_id, "新分段问题")
            memory.record_assistant(result.session_id, "新分段回答")
            second_provider = CompressionProvider()
            second = asyncio.run(ContextProcessor(
                config, memory, provider_factory=lambda: second_provider,
            ).compress(result.session_id))
            self.assertEqual(second.status, "compressed")
            self.assertTrue(memory.active_filename(result.session_id).endswith("_003.jsonl"))
            updated = json.loads((config.memory_dir / "profile" / "index.json").read_text(encoding="utf-8"))["profiles"][result.session_id]
            self.assertEqual(updated["segments_processed"], 2)
            self.assertEqual(updated["conversation_turns"], 2)

    def test_manual_compress_is_not_recorded_and_only_returns_status(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            session_id = memory.create_session("第一句")
            memory.record_user(session_id, "第一句")
            memory.record_assistant(session_id, "第一答")
            compressor = CompressionProvider()
            runtime = AgentRuntime(
                config,
                provider=UsageProvider(),
                memory=memory,
                compression_provider_factory=lambda: compressor,
            )
            result = asyncio.run(runtime.run("/compress", session_id))
            self.assertTrue(result.completed)
            self.assertIn("上下文压缩完成", result.answer)
            contents = [record.get("content") for record in memory.session_records(session_id)]
            self.assertNotIn("/compress", contents)
            self.assertEqual(memory.session_records(session_id)[0]["role"], "summary")

    def test_compression_retries_three_times_then_trims_only_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, compression_threshold_tokens=80)
            memory = MemoryStore(config.memory_dir)
            session_id = memory.create_session("旧问题")
            memory.record_user(session_id, "旧问题" * 60)
            memory.record_assistant(session_id, "旧回答" * 60)
            compressor = CompressionProvider(valid=False)
            processor = ContextProcessor(config, memory, provider_factory=lambda: compressor)
            result = asyncio.run(processor.compress(session_id))
            self.assertEqual(result.status, "fallback")
            self.assertEqual(compressor.calls, 3)
            self.assertTrue(memory.active_filename(session_id).endswith("_001.jsonl"))
            original = memory.session_records(session_id)
            messages = [
                {"role": "system", "content": "规则"},
                {"role": "user", "content": "旧问题" * 60},
                {"role": "assistant", "content": "旧回答" * 60},
                {"role": "user", "content": "新问题"},
            ]
            self.assertTrue(processor.trim_messages_if_needed(session_id, messages))
            self.assertEqual([item["role"] for item in messages], ["system", "user"])
            self.assertEqual(messages[-1]["content"], "新问题")
            self.assertEqual(memory.session_records(session_id), original)

    def test_hash_profiles_are_isolated_between_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            first, second = "a" * 16, "b" * 16
            (memory.profiles.directory / f"{first}.md").write_text("第一会话特点", encoding="utf-8")
            (memory.profiles.directory / f"{second}.md").write_text("第二会话特点", encoding="utf-8")
            context = memory.profile_context(first)
            self.assertTrue(context.startswith(f"[{first}]"))
            self.assertIn("第一会话特点", context)
            self.assertNotIn("第二会话特点", context)

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
                context = ToolContext(project_root=Path(value))
                tools = default_tools(Path(value))
                with self.assertRaises(PermissionError):
                    await tools.execute("write_file", {"path": "note.txt", "content": "x"}, context)
                with self.assertRaises(PermissionError):
                    await tools.execute("read_file", {"path": "../secret.txt"}, context)
        asyncio.run(check())

    def test_default_tools_are_independent_modules_and_all_execute(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                root = Path(value)

                async def approve(name, arguments) -> bool:
                    return True

                registry = default_tools(root)
                self.assertEqual(
                    {schema["name"] for schema in registry.schemas()},
                    {"read_file", "write_file", "calculator", "search_workspace", "current_time"},
                )
                context = ToolContext(project_root=root, approval=approve)
                await registry.execute("write_file", {"path": "notes/demo.txt", "content": "独立工具模块"}, context)
                self.assertEqual(
                    await registry.execute("read_file", {"path": "notes/demo.txt"}, context),
                    "独立工具模块",
                )
                self.assertEqual(
                    await registry.execute("calculator", {"expression": "(10 + 20) / 2"}, context),
                    "15.0",
                )
                self.assertIn(
                    "demo.txt",
                    await registry.execute("search_workspace", {"query": "独立工具模块"}, context),
                )
                self.assertIn("T", await registry.execute("current_time", {}, context))

        asyncio.run(check())

    def test_tool_arguments_use_strict_pydantic_validation(self) -> None:
        """模型工具参数不得被隐式转换，也不得携带 Schema 外字段。"""
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                registry = default_tools(Path(value))
                context = ToolContext(project_root=Path(value))
                with self.assertRaisesRegex(ValueError, "工具参数校验失败"):
                    await registry.execute("calculator", {"expression": 4}, context)
                with self.assertRaisesRegex(ValueError, "工具参数校验失败"):
                    await registry.execute("calculator", {"expression": "2+2", "unexpected": True}, context)

        asyncio.run(check())

    def test_subagent_tool_defaults_to_no_tools_and_uses_two_stage_write_approval(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                root = Path(value)
                approvals: list[str] = []

                async def approve(name, arguments) -> bool:
                    approvals.append(name)
                    return True

                captured: list[list[str]] = []

                async def runner(task, instructions, names, context) -> str:
                    captured.append(names)
                    if "write_file" in names:
                        selected = default_tools(root).select(names)
                        return await selected.execute(
                            "write_file", {"path": "delegated.txt", "content": task}, context,
                        )
                    return f"子任务完成：{task}"

                registry = default_tools(root, subagent_runner=runner)
                context = ToolContext(project_root=root, approval=approve)
                self.assertEqual(
                    await registry.execute("subagent", {"task": "分析"}, context),
                    "子任务完成：分析",
                )
                self.assertEqual(captured[-1], [])
                self.assertEqual(approvals, [])
                await registry.execute(
                    "subagent",
                    {"task": "内容", "instructions": "负责写入", "tools": ["write_file"]},
                    context,
                )
                self.assertEqual(approvals, ["subagent", "write_file"])
                self.assertEqual((root / "delegated.txt").read_text(encoding="utf-8"), "内容")
                with self.assertRaises(ValueError):
                    await registry.execute("subagent", {"task": "递归", "tools": ["subagent"]}, context)

        asyncio.run(check())

    def test_subagent_only_persists_as_parent_tool_chain(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)

            async def runner(task, instructions, names, context) -> str:
                self.assertEqual(names, [])
                return "子 Agent 结论"

            runtime = AgentRuntime(
                config,
                provider=SubagentCallingProvider(),
                memory=memory,
                subagent_runner=runner,
            )
            result = asyncio.run(runtime.run("委派任务"))
            self.assertTrue(result.completed)
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant", "tool", "assistant"])
            self.assertEqual(records[2]["name"], "subagent")
            self.assertEqual(records[2]["content"], "子 Agent 结论")
            session_index = json.loads((config.memory_dir / "session" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(list(session_index["sessions"]), [result.session_id])
