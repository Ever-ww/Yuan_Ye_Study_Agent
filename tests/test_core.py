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
from Agent.models.providers import _http_client_options, build_provider
from Agent.runtime.subagent import RuntimeSubagentRunner
from bootstrap import ensure_project_initialized, is_project_initialized
from context_process import ContextProcessor
from memory import HarnessLongTermMemory, MemoryStore
from prompt import PromptComposer
from sandbox import WorkspaceLockManager
from tool import AsyncToolRegistry, ToolContext, default_tools


class ToolProvider:
    """先请求计算工具、再依据 Observation 完成的测试模型。"""

    async def complete(self, messages, tools):
        if not any(message["role"] == "tool" for message in messages):
            return ModelReply(tool_calls=(ToolCall(name="calculator", arguments={"expression": "2 + 2"}),), finished=False)
        return ModelReply(text="计算完成：4")


class _RestartTool:
    name = "restart_tool"
    description = "test terminal Tool"
    risk = "read"
    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, arguments, context):
        del arguments, context
        return json.dumps({"status": "merged", "restart_required": True})

    @staticmethod
    def ends_turn(result: str) -> bool:
        return bool(json.loads(result).get("restart_required"))


class _RestartProvider:
    streaming = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools):
        del messages, tools
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("restart-required Tool must terminate before another model call")
        return ModelReply(tool_calls=(ToolCall(name="restart_tool", arguments={}),))


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
        failures = [message for message in messages if message["role"] == "tool"]
        if failures:
            return ModelReply(text="读取失败后已使用模型自身知识继续回答")
        return ModelReply(tool_calls=(ToolCall(name="read_file", arguments={"path": "missing-file.txt"}),))


class InvalidWriteProvider:
    """写工具请求校验失败后，根据结构化 observation 继续完成回答。"""

    streaming = False

    async def complete(self, messages, tools):
        failures = [message for message in messages if message["role"] == "tool"]
        if failures:
            return ModelReply(text="已根据参数错误重新规划")
        return ModelReply(tool_calls=(ToolCall(
            name="validated_write",
            arguments={"value": "invalid"},
        ),))


class ValidatedWriteTool:
    name = "validated_write"
    description = "测试真实副作用前的语义校验"
    risk = "write"
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def ensure_available(self, arguments, context) -> None:
        del arguments, context
        raise ValueError("authors 必须使用对象格式")

    async def run(self, arguments, context) -> str:  # pragma: no cover - 不应执行
        raise AssertionError("请求校验失败后不得执行真实写入")


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


class SummaryOnlyCompressionProvider:
    """Harness 压缩不允许生成 Session Profile。"""

    streaming = False

    async def complete(self, messages, tools):
        del messages, tools
        return ModelReply(text=json.dumps({
            "context_summary_markdown": "# 用户目标\n修复项目\n# 已完成任务\n定位\n# 未完成任务\n修改\n# 关键决策\n最小改动\n# 必要工具结论\n无",
        }, ensure_ascii=False))


class LargeUsageProvider:
    """用精确 usage 触发自动压缩。"""

    streaming = False

    async def complete(self, messages, tools):
        return ModelReply(text="已完成大上下文回答", usage=TokenUsage(input_tokens=20000, output_tokens=20))


class ToolThenFinalProvider:
    """先调用工具，再验证压缩后的下一次请求仍能完成。"""

    streaming = False

    def __init__(self) -> None:
        self.calls = 0
        self.second_messages = []

    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(tool_calls=(
                ToolCall(name="calculator", arguments={"expression": "2 + 2"}),
            ))
        self.second_messages = [dict(message) for message in messages]
        return ModelReply(text="压缩后继续完成：4")


class CapturingProvider:
    """保存最终发送消息，用于验证记忆由 Hook 注入。"""

    streaming = False

    def __init__(self) -> None:
        self.messages = []

    async def complete(self, messages, tools):
        self.messages = [dict(message) for message in messages]
        return ModelReply(text="第二答")


class _CheckpointSandbox:
    """不涉及 Docker 的工具单测 checkpoint 替身。"""

    async def checkpoint_write(self, path: str):
        del path
        return type("Checkpoint", (), {"commit_sha": "0" * 40})()

    async def restore_current(self):
        return None


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
            self.assertTrue((yy / "skills" / "index.json").exists())
            self.assertTrue((yy / "skills" / "review").is_dir())
            self.assertFalse((yy / "skills" / "installed").exists())
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

    def test_model_proxy_defaults_to_direct_and_supports_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            direct = load_runtime_config(root)
            self.assertFalse(direct.use_system_proxy)
            self.assertIsNone(direct.proxy_url)
            self.assertEqual(
                _http_client_options(
                    60,
                    use_system_proxy=direct.use_system_proxy,
                    proxy_url=direct.proxy_url,
                ),
                {"timeout": 60, "trust_env": False},
            )

            local = root / ".yy" / "settings.local.json"
            local.write_text('{"use_system_proxy":true}', encoding="utf-8")
            system = load_runtime_config(root)
            self.assertTrue(system.use_system_proxy)
            self.assertEqual(
                _http_client_options(
                    60,
                    use_system_proxy=system.use_system_proxy,
                    proxy_url=system.proxy_url,
                ),
                {"timeout": 60, "trust_env": True},
            )

            local.write_text(
                '{"use_system_proxy":false,"proxy_url":"http://127.0.0.1:7890"}',
                encoding="utf-8",
            )
            explicit = load_runtime_config(root)
            provider = build_provider(
                "deepseek",
                "deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                api_key="test-key",
                use_system_proxy=explicit.use_system_proxy,
                proxy_url=explicit.proxy_url,
            )
            self.assertFalse(provider.use_system_proxy)
            self.assertEqual(provider.proxy_url, "http://127.0.0.1:7890")
            self.assertEqual(
                _http_client_options(
                    60,
                    use_system_proxy=explicit.use_system_proxy,
                    proxy_url=explicit.proxy_url,
                ),
                {
                    "timeout": 60,
                    "trust_env": False,
                    "proxy": "http://127.0.0.1:7890",
                },
            )

    def test_model_proxy_configuration_rejects_ambiguous_or_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            local = root / ".yy" / "settings.local.json"
            load_runtime_config(root)
            local.write_text(
                '{"use_system_proxy":true,"proxy_url":"http://127.0.0.1:7890"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "不能同时启用"):
                load_runtime_config(root)
            local.write_text(
                '{"proxy_url":"socks5://127.0.0.1:1080"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "只支持 http"):
                load_runtime_config(root)

    def test_config_keeps_memory_in_agent_root_and_workspace_clean(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            agent_root = base / "agent"
            workspace = base / "workspace"
            agent_root.mkdir()
            workspace.mkdir()

            config = load_runtime_config(agent_root, workspace_root=workspace)

            self.assertEqual(config.agent_root, agent_root.resolve())
            self.assertEqual(config.workspace_root, workspace.resolve())
            self.assertEqual(config.memory_dir, agent_root / ".yy" / "memory")
            self.assertTrue((agent_root / ".yy" / "settings.local.json").exists())
            self.assertFalse((workspace / ".yy").exists())

    def test_sessions_are_workspace_scoped_but_profiles_are_shared(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            agent_root = base / "agent"
            first_workspace = base / "first"
            second_workspace = base / "second"
            for path in (agent_root, first_workspace, second_workspace):
                path.mkdir()
            memory_root = agent_root / ".yy" / "memory"
            first = MemoryStore(
                memory_root,
                workspace_root=first_workspace,
                agent_root=agent_root,
            )
            second = MemoryStore(
                memory_root,
                workspace_root=second_workspace,
                agent_root=agent_root,
            )

            session_id = first.create_session("第一工作区问题")
            first.record_user(session_id, "第一工作区问题")
            first.profiles.directory.joinpath("USER.md").write_text(
                "用户偏好中文回答",
                encoding="utf-8",
            )

            self.assertTrue(first.has_session(session_id))
            self.assertFalse(second.has_session(session_id))
            with self.assertRaises(KeyError):
                second.restore_messages(session_id)
            self.assertNotIn(session_id, {item["session_id"] for item in second.list_sessions()})
            self.assertIn("用户偏好中文回答", second.profile_context())
            session_indexes = list((memory_root / "session").glob("*/index.json"))
            self.assertEqual(len(session_indexes), 2)
            self.assertFalse((first_workspace / ".yy").exists())
            self.assertFalse((second_workspace / ".yy").exists())
            config = load_runtime_config(agent_root, workspace_root=first_workspace)
            with self.assertRaisesRegex(ValueError, "MemoryStore.workspace_root"):
                AgentRuntime(config, memory=second, enable_sandbox=False)

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
            defaults = load_runtime_config(root)
            self.assertEqual(defaults.compression_threshold_tokens, 200000)
            self.assertEqual(defaults.sandbox_checkpoint_limit, 17)
            self.assertEqual(defaults.sandbox_checkpoint_merged_branch_retention_days, 30)
            self.assertEqual(defaults.approval_timeout_seconds, 30)
            (root / ".yy" / "settings.local.json").write_text(
                '{"compression_threshold_tokens":-1}', encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "compression_threshold_tokens"):
                load_runtime_config(root)
            (root / ".yy" / "settings.local.json").write_text(
                '{"sandbox_checkpoint_limit":0}', encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "sandbox_checkpoint_limit"):
                load_runtime_config(root)
            (root / ".yy" / "settings.local.json").write_text(
                '{"sandbox_checkpoint_merged_branch_retention_days":0}', encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "sandbox_checkpoint_merged_branch_retention_days",
            ):
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
            runtime = AgentRuntime(
                load_runtime_config(root), provider=provider, memory=memory, enable_sandbox=False,
            )
            asyncio.run(runtime.run("第二句", session_id))
            self.assertEqual(
                [(item["role"], item["content"]) for item in provider.messages[1:-1]],
                [("user", "第一句"), ("assistant", "第一答")],
            )
            self.assertTrue(provider.messages[-1]["content"].startswith("第二句\n\n[本次提问时间："))
            self.assertTrue(PromptComposer(root).compose("纯基础")[1]["content"].startswith("纯基础\n\n[本次提问时间："))

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
            runtime = AgentRuntime(
                config, provider=ToolProvider(), memory=memory, enable_sandbox=False,
            )
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
            result = asyncio.run(AgentRuntime(
                config, provider=ToolProvider(), memory=memory, enable_sandbox=False,
            ).run("计算 2 + 2"))
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant", "tool", "assistant"])
            call = records[1]["tool_calls"][0]
            self.assertEqual(call["function"]["name"], "calculator")
            self.assertTrue(call["id"].startswith("call_"))
            self.assertEqual(records[2]["tool_call_id"], call["id"])
            self.assertEqual(records[2]["status"], "success")
            restored = memory.restore_messages(result.session_id)
            self.assertEqual(restored[1]["tool_calls"][0]["id"], restored[2]["tool_call_id"])

    def test_restore_repairs_incomplete_multi_tool_chain_without_changing_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            session_id = memory.create_session("research")
            memory.record_user(session_id, "research")
            calls = [
                {
                    "id": f"call_{index}", "type": "function",
                    "function": {"name": f"tool_{index}", "arguments": "{}"},
                }
                for index in range(3)
            ]
            memory.record_model_tool_calls(
                session_id, content=None, tool_calls=calls, model={}, model_call={},
            )
            memory.record_tool_result(
                session_id, tool_call_id="call_0", name="tool_0",
                content="failed", status="error", arguments={},
            )
            memory.record_user(session_id, "continue")

            restored = memory.refresh_messages(session_id)
            assistant_index = next(
                index for index, item in enumerate(restored)
                if item.get("role") == "assistant" and item.get("tool_calls")
            )
            following = restored[assistant_index + 1 : assistant_index + 4]
            self.assertEqual([item["role"] for item in following], ["tool", "tool", "tool"])
            self.assertEqual(
                {item["tool_call_id"] for item in following},
                {"call_0", "call_1", "call_2"},
            )
            self.assertIn("执行结果未知", following[1]["content"])
            self.assertEqual(restored[-1]["role"], "assistant")
            self.assertFalse(any(
                left.get("role") == right.get("role") == "user"
                for left, right in zip(restored, restored[1:])
            ))
            self.assertEqual(
                sum(item["role"] == "tool" for item in memory.session_records(session_id)),
                1,
            )

    def test_turn_failure_closes_pending_tools_before_terminal_marker(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            session_id = memory.create_session("research")
            memory.record_user(session_id, "research")
            calls = [
                {
                    "id": f"call_{index}", "type": "function",
                    "function": {"name": f"tool_{index}", "arguments": "{}"},
                }
                for index in range(2)
            ]
            memory.record_model_tool_calls(
                session_id, content=None, tool_calls=calls, model={}, model_call={},
            )
            memory.record_tool_result(
                session_id, tool_call_id="call_0", name="tool_0",
                content="failed", status="error", arguments={},
            )
            self.assertTrue(memory.record_turn_failure(session_id, "field error"))
            records = memory.session_records(session_id)
            self.assertEqual([item["role"] for item in records[-2:]], ["tool", "assistant"])
            self.assertEqual(records[-2]["status"], "skipped")
            self.assertEqual(records[-2]["tool_call_id"], "call_1")
            self.assertEqual(records[-1]["status"], "error")

    def test_failed_read_tool_is_persisted_and_model_can_continue(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            result = asyncio.run(AgentRuntime(
                config, provider=FailingToolProvider(), memory=memory, enable_sandbox=False,
            ).run("读取缺失文件"))
            self.assertTrue(result.completed)
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant", "tool", "assistant"])
            self.assertEqual(records[-2]["status"], "error")
            self.assertIn("工具执行失败", records[-2]["content"])
            self.assertEqual(records[-1]["content"], "读取失败后已使用模型自身知识继续回答")

    def test_write_request_validation_error_is_observed_and_model_can_continue(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            runtime = AgentRuntime(
                config,
                provider=InvalidWriteProvider(),
                tools=AsyncToolRegistry((ValidatedWriteTool(),)),
                memory=memory,
                approval=lambda name, arguments: asyncio.sleep(0, result=True),
                enable_sandbox=False,
                enable_subagent=False,
                enable_skills=False,
                enable_references=False,
                enable_paper_library=False,
            )
            result = asyncio.run(runtime.run("写入论文元数据"))
            self.assertTrue(result.completed)
            records = memory.session_records(result.session_id)
            self.assertEqual([item["role"] for item in records], ["user", "assistant", "tool", "assistant"])
            self.assertEqual(records[-2]["status"], "error")
            self.assertIn("authors 必须使用对象格式", records[-2]["content"])
            self.assertEqual(records[-1]["content"], "已根据参数错误重新规划")

    def test_automatic_compression_merges_profile_and_rolls_over(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, compression_threshold_tokens=300)
            memory = MemoryStore(config.memory_dir)
            session_id = memory.create_session("历史问题")
            memory.record_user(session_id, "历史问题" * 300)
            memory.record_assistant(session_id, "历史回答" * 300)
            compressor = CompressionProvider()
            provider = CapturingProvider()
            runtime = AgentRuntime(
                config,
                provider=provider,
                memory=memory,
                compression_provider_factory=lambda: compressor,
                enable_sandbox=False,
            )
            result = asyncio.run(runtime.run("整理长上下文", session_id))
            self.assertTrue(result.completed)
            self.assertEqual(compressor.calls, 1)
            self.assertTrue(memory.active_filename(result.session_id).endswith("_002.jsonl"))
            active_records = memory.session_records(result.session_id)
            summary = active_records[0]
            self.assertEqual(summary["role"], "summary")
            self.assertEqual([record["role"] for record in active_records], [
                "summary",
                "user",
                "assistant",
            ])
            self.assertEqual(active_records[1]["content"], "整理长上下文")
            self.assertEqual(
                sum(record.get("content") == "整理长上下文" for record in active_records),
                1,
            )
            self.assertEqual(memory.restore_messages(result.session_id)[0]["role"], "user")
            self.assertTrue(provider.messages[-1]["content"].startswith("整理长上下文\n\n[本次提问时间："))
            compression_payload = json.loads(compressor.messages[-1]["content"])
            self.assertNotIn(
                "整理长上下文",
                json.dumps(compression_payload["session_records"], ensure_ascii=False),
            )
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
            self.assertEqual(updated["conversation_turns"], 3)

    def test_tool_chain_is_compressed_before_the_followup_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, compression_threshold_tokens=80)
            memory = MemoryStore(config.memory_dir)
            compressor = CompressionProvider()
            provider = ToolThenFinalProvider()
            runtime = AgentRuntime(
                config,
                provider=provider,
                memory=memory,
                compression_provider_factory=lambda: compressor,
                enable_sandbox=False,
            )
            result = asyncio.run(runtime.run("计算 2 + 2"))
            self.assertTrue(result.completed)
            self.assertEqual(provider.calls, 2)
            self.assertEqual(compressor.calls, 1)
            self.assertTrue(memory.active_filename(result.session_id).endswith("_002.jsonl"))
            payload = json.loads(compressor.messages[-1]["content"])
            roles = [record["role"] for record in payload["session_records"]]
            self.assertEqual(roles, ["user", "assistant", "tool"])
            self.assertIn("tool_calls", payload["session_records"][1])
            self.assertEqual(
                [message["role"] for message in provider.second_messages],
                ["system"],
            )

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
                enable_sandbox=False,
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

    def test_harness_compression_rolls_session_without_hash_profile(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            memory_root = root / ".yy" / "harness-evolution" / "memory"
            profiles = HarnessLongTermMemory(memory_root / "profile", agent_root=root)
            memory = MemoryStore(
                memory_root,
                workspace_root=root,
                agent_root=root,
                partition_by_workspace=False,
                profiles=profiles,
            )
            session_id = memory.create_session("修复问题")
            memory.record_user(session_id, "修复问题")
            memory.record_assistant(session_id, "开始定位")
            config = load_runtime_config(root)
            result = asyncio.run(ContextProcessor(
                config,
                memory,
                provider_factory=SummaryOnlyCompressionProvider,
            ).compress(session_id))
            self.assertEqual(result.status, "compressed")
            self.assertIsNone(result.profile_file)
            self.assertTrue(memory.active_filename(session_id).endswith("_002.jsonl"))
            self.assertFalse((profiles.directory / f"{session_id}.md").exists())
            self.assertEqual(
                {path.name for path in profiles.directory.glob("*.md")},
                {"AGENT.md", "PROJECT.md", "CHANGES.md", "LESSONS.md"},
            )

    def test_harness_long_term_prompt_keeps_core_and_newest_log_entries(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            profiles = HarnessLongTermMemory(
                root / ".yy" / "harness-evolution" / "memory" / "profile",
                agent_root=root,
            )
            profiles.initialize()
            (profiles.directory / "AGENT.md").write_text(
                "# 用户规则\n始终保留这条规则\n",
                encoding="utf-8",
            )
            entries = [
                f"## 2026-07-{index:02d} - 更新 {index}\n\n" + ("内容" * 1800)
                for index in range(1, 25)
            ]
            (profiles.directory / "CHANGES.md").write_text(
                "# Verified Changes\n\n" + "\n\n".join(entries) + "\n",
                encoding="utf-8",
            )
            context = profiles.load_for_session("a" * 16)
            self.assertLessEqual(len(context), 64 * 1024)
            self.assertIn("始终保留这条规则", context)
            self.assertIn("更新 24", context)
            self.assertNotIn("更新 1\n", context)

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
            runtime = AgentRuntime(
                config, provider=UsageProvider(), memory=memory, enable_sandbox=False,
            )
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
            runtime = AgentRuntime(
                load_runtime_config(Path(value)), provider=ToolProvider(), hooks=hooks,
                enable_sandbox=False,
            )
            result = asyncio.run(runtime.run("计算 2 + 2"))
            self.assertTrue(result.completed)
            self.assertEqual(points, [
                "trace_start",
                "turn_start", "model_before", "model_during", "model_after",
                "tool_before", "tool_during", "tool_after",
                "model_before", "model_during", "model_after", "turn_end",
                "trace_end",
            ])

    def test_one_turn_can_execute_multiple_tools(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            tool_events: list[HookEvent] = []
            hooks = HookRegistry()

            async def observe(event: HookEvent) -> None:
                tool_events.append(event)

            hooks.register(HookPoint.TOOL_BEFORE, observe)
            runtime = AgentRuntime(
                load_runtime_config(Path(value)), provider=MultiToolProvider(), hooks=hooks,
                enable_sandbox=False,
            )
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
            runtime = AgentRuntime(
                load_runtime_config(Path(value)), provider=ToolProvider(), hooks=hooks,
                enable_sandbox=False,
            )
            result = asyncio.run(runtime.run("计算"))
            self.assertTrue(result.completed)
            self.assertEqual(result.answer, "计算完成：4")

    def test_session_hooks_do_not_expose_turn_numbers(self) -> None:
        async def check() -> list[HookEvent]:
            with tempfile.TemporaryDirectory() as value:
                events: list[HookEvent] = []
                hooks = HookRegistry()

                async def observe(event: HookEvent) -> None:
                    events.append(event)

                hooks.register(HookPoint.MODEL_AFTER, observe)
                runtime = AgentRuntime(
                    load_runtime_config(Path(value)), provider=UsageProvider(), hooks=hooks,
                    enable_sandbox=False,
                )
                await runtime.run("问题")
                return events

        events = asyncio.run(check())
        self.assertEqual(len(events), 1)
        self.assertFalse(hasattr(events[0], "turn"))
        self.assertNotIn("turn", events[0].data["model_call"])

    def test_runtime_emits_streaming_text_events_in_order(self) -> None:
        async def collect() -> list[str]:
            with tempfile.TemporaryDirectory() as value:
                runtime = AgentRuntime(
                    load_runtime_config(Path(value)), provider=StreamProvider(), enable_sandbox=False,
                )
                return [str(event.payload["content"]) async for event in runtime.run_task("问候") if event.type is EventType.TEXT]
        self.assertEqual(asyncio.run(collect()), ["你", "好"])

    def test_restart_required_tool_finishes_without_second_model_call(self) -> None:
        async def collect():
            with tempfile.TemporaryDirectory() as value:
                provider = _RestartProvider()
                runtime = AgentRuntime(
                    load_runtime_config(Path(value)), provider=provider,
                    tools=AsyncToolRegistry([_RestartTool()]), enable_sandbox=False,
                )
                events = [event async for event in runtime.run_task("补能力")]
                return provider.calls, events

        calls, events = asyncio.run(collect())
        self.assertEqual(calls, 1)
        self.assertIn(EventType.GATEWAY_RESTART_REQUIRED, [event.type for event in events])
        self.assertEqual(events[-1].type, EventType.FINAL)
        self.assertIn("重启 Gateway", events[-1].payload["answer"])

    def test_write_tool_requires_approval_and_stays_in_workspace(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                context = ToolContext(project_root=Path(value))
                tools = default_tools(Path(value))
                with self.assertRaises(PermissionError):
                    await tools.execute("write", {"path": "note.txt", "content": "x"}, context)
                with self.assertRaises(PermissionError):
                    await tools.execute("read_file", {"path": "../secret.txt"}, context)
        asyncio.run(check())

    def test_tool_framework_is_separate_from_callable_tool_modules(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        for filename in ("contracts.py", "registry.py", "defaults.py", "path_guard.py"):
            self.assertTrue((source_root / "tool" / filename).is_file())
            self.assertFalse((source_root / "tools" / filename).exists())

    def test_default_tools_are_independent_modules_and_all_execute(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                root = Path(value)

                async def approve(name, arguments) -> bool:
                    return True

                registry = default_tools(root)
                self.assertEqual(
                    {schema["name"] for schema in registry.schemas()},
                    {
                        "read_file",
                        "edit",
                        "write",
                        "bash",
                        "sandbox_rollback",
                        "sandbox_checkpoint_history",
                        "sandbox_checkpoint_branch",
                        "calculator",
                        "search_workspace",
                        "current_time",
                        "profile_read",
                    },
                )
                context = ToolContext(
                    project_root=root,
                    approval=approve,
                    sandbox=_CheckpointSandbox(),
                    file_locks=WorkspaceLockManager(root),
                )
                await registry.execute("write", {"path": "notes/demo.txt", "content": "独立工具模块"}, context)
                await registry.execute(
                    "edit",
                    {
                        "path": "notes/demo.txt",
                        "edits": [{"oldText": "独立", "newText": "精确编辑"}],
                    },
                    context,
                )
                self.assertEqual(
                    await registry.execute("read_file", {"path": "notes/demo.txt"}, context),
                    "精确编辑工具模块",
                )
                self.assertEqual(
                    await registry.execute("calculator", {"expression": "(10 + 20) / 2"}, context),
                    "15.0",
                )
                self.assertIn(
                    "demo.txt",
                    await registry.execute("search_workspace", {"query": "精确编辑工具模块"}, context),
                )
                self.assertIn("T", await registry.execute("current_time", {}, context))

        asyncio.run(check())

    def test_edit_uses_original_text_for_multiple_exact_replacements(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                root = Path(value)
                path = root / "demo.txt"
                path.write_text("one two\n", encoding="utf-8")

                async def approve(name, arguments) -> bool:
                    del name, arguments
                    return True

                registry = default_tools(root)
                context = ToolContext(
                    project_root=root,
                    approval=approve,
                    sandbox=_CheckpointSandbox(),
                    file_locks=WorkspaceLockManager(root),
                )
                result = await registry.execute(
                    "edit",
                    {
                        "path": "demo.txt",
                        "edits": [
                            {"oldText": "one", "newText": "two"},
                            {"oldText": "two", "newText": "three"},
                        ],
                    },
                    context,
                )
                self.assertEqual(path.read_text(encoding="utf-8"), "two three\n")
                self.assertIn("checkpoint", result)

        asyncio.run(check())

    def test_edit_preserves_bom_and_crlf_and_normalizes_pi_arguments(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                root = Path(value)
                path = root / "demo.txt"
                path.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\n")

                async def approve(name, arguments) -> bool:
                    del name, arguments
                    return True

                registry = default_tools(root)
                context = ToolContext(
                    project_root=root,
                    approval=approve,
                    sandbox=_CheckpointSandbox(),
                    file_locks=WorkspaceLockManager(root),
                )
                await registry.execute(
                    "edit",
                    {
                        "path": "demo.txt",
                        "edits": json.dumps(
                            [{"oldText": "alpha\nbeta", "newText": "first\nsecond"}],
                        ),
                    },
                    context,
                )
                self.assertEqual(
                    path.read_bytes(),
                    b"\xef\xbb\xbffirst\r\nsecond\r\n",
                )

        asyncio.run(check())

    def test_edit_nested_schema_is_validated_before_approval(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                approvals: list[str] = []

                async def approve(name, arguments) -> bool:
                    del arguments
                    approvals.append(name)
                    return True

                root = Path(value)
                (root / "demo.txt").write_text("before", encoding="utf-8")
                registry = default_tools(root)
                context = ToolContext(
                    project_root=root,
                    approval=approve,
                    sandbox=_CheckpointSandbox(),
                    file_locks=WorkspaceLockManager(root),
                )
                with self.assertRaisesRegex(ValueError, "工具参数校验失败"):
                    await registry.execute(
                        "edit",
                        {
                            "path": "demo.txt",
                            "edits": [{"oldText": "before"}],
                        },
                        context,
                    )
                with self.assertRaisesRegex(ValueError, "工具参数校验失败"):
                    await registry.execute(
                        "edit",
                        {
                            "path": "demo.txt",
                            "edits": [{
                                "oldText": "before",
                                "newText": "after",
                                "unexpected": True,
                            }],
                        },
                        context,
                    )
                self.assertEqual(approvals, [])

        asyncio.run(check())

    def test_edit_rejects_missing_duplicate_and_overlapping_matches(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as value:
                root = Path(value)
                path = root / "demo.txt"

                async def approve(name, arguments) -> bool:
                    del name, arguments
                    return True

                registry = default_tools(root)
                context = ToolContext(
                    project_root=root,
                    approval=approve,
                    sandbox=_CheckpointSandbox(),
                    file_locks=WorkspaceLockManager(root),
                )
                path.write_text("repeat repeat", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "不是唯一匹配"):
                    await registry.execute(
                        "edit",
                        {
                            "path": "demo.txt",
                            "edits": [{"oldText": "repeat", "newText": "once"}],
                        },
                        context,
                    )
                with self.assertRaisesRegex(ValueError, "不存在"):
                    await registry.execute(
                        "edit",
                        {
                            "path": "demo.txt",
                            "edits": [{"oldText": "missing", "newText": "value"}],
                        },
                        context,
                    )
                path.write_text("abcdef", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "重叠或嵌套"):
                    await registry.execute(
                        "edit",
                        {
                            "path": "demo.txt",
                            "edits": [
                                {"oldText": "abc", "newText": "x"},
                                {"oldText": "bcde", "newText": "y"},
                            ],
                        },
                        context,
                    )

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
                    if "write" in names:
                        selected = default_tools(root).select(names)
                        return await selected.execute(
                            "write", {"path": "delegated.txt", "content": task}, context,
                        )
                    return f"子任务完成：{task}"

                registry = default_tools(root, subagent_runner=runner)
                context = ToolContext(
                    project_root=root,
                    approval=approve,
                    sandbox=_CheckpointSandbox(),
                    file_locks=WorkspaceLockManager(root),
                )
                self.assertEqual(registry.risk_of("subagent"), "dynamic")
                self.assertEqual(registry.risk_of("subagent", {"task": "分析"}), "read")
                self.assertEqual(
                    registry.risk_of("subagent", {"task": "写入", "tools": ["write"]}),
                    "write",
                )
                with self.assertRaisesRegex(ValueError, "工具参数校验失败"):
                    registry.risk_of("subagent", {"task": "越权", "tools": ["unknown_tool"]})
                self.assertEqual(
                    await registry.execute("subagent", {"task": "分析"}, context),
                    "子任务完成：分析",
                )
                self.assertEqual(captured[-1], [])
                self.assertEqual(approvals, [])
                await registry.execute(
                    "subagent",
                    {"task": "内容", "instructions": "负责写入", "tools": ["write"]},
                    context,
                )
                self.assertEqual(approvals, ["subagent", "write"])
                self.assertEqual((root / "delegated.txt").read_text(encoding="utf-8"), "内容")
                with self.assertRaises(ValueError):
                    await registry.execute("subagent", {"task": "递归", "tools": ["subagent"]}, context)

        asyncio.run(check())

    def test_subagent_runtime_reuses_context_without_session_persistence(self) -> None:
        """真实临时 Runtime 复用父上下文，且不会创建独立 Session。"""
        async def check(root: Path) -> None:
            config = load_runtime_config(root)
            context = ToolContext(
                project_root=root,
                file_locks=WorkspaceLockManager(root),
            )
            runner = RuntimeSubagentRunner(config, default_tools(root))
            result = await runner("独立分析", "保持简洁", [], context)
            self.assertIn("独立分析", result)
            index = json.loads((config.memory_dir / "session" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["sessions"], {})

            runtime = AgentRuntime(
                config,
                provider=UsageProvider(),
                tool_context=context,
                enable_subagent=False,
                enable_sandbox=False,
            )
            self.assertIs(runtime.tool_context, context)
            await runtime.close()

            outside_context = ToolContext(project_root=root / "other-workspace")
            with self.assertRaisesRegex(ValueError, "工作区"):
                AgentRuntime(
                    config,
                    provider=UsageProvider(),
                    tool_context=outside_context,
                    enable_subagent=False,
                    enable_sandbox=False,
                )

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

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
                enable_sandbox=False,
            )
            result = asyncio.run(runtime.run("委派任务"))
            self.assertTrue(result.completed)
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant", "tool", "assistant"])
            self.assertEqual(records[2]["name"], "subagent")
            self.assertEqual(records[2]["content"], "子 Agent 结论")
            session_index = json.loads((config.memory_dir / "session" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(list(session_index["sessions"]), [result.session_id])

    def test_system_prompt_is_single_cached_string_and_skill_xml_is_first(self) -> None:
        class CapturingProvider:
            streaming = False

            def __init__(self) -> None:
                self.messages = []

            async def complete(self, messages, tools):
                self.messages = [dict(message) for message in messages]
                return ModelReply(text="完成")

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            (root / "AGENT.md").write_text("根目录不得进入模型", encoding="utf-8")
            (root / ".yy" / "agents" / "SOUL.md").write_text("身份内容", encoding="utf-8")
            (root / ".yy" / "agents" / "AGENT.md").write_text("项目内容", encoding="utf-8")
            provider = CapturingProvider()
            runtime = AgentRuntime(config, provider=provider, enable_sandbox=False)
            result = asyncio.run(runtime.run("测试问题"))
            self.assertTrue(result.completed)
            self.assertEqual([item["role"] for item in provider.messages].count("system"), 1)
            system = provider.messages[0]["content"]
            self.assertTrue(system.startswith("<available_skills>"))
            self.assertLess(system.index("身份内容"), system.index("项目内容"))
            self.assertLess(system.index("身份内容"), system.index("# Skill 使用策略"))
            self.assertLess(system.index("# Skill 使用策略"), system.index("# 核心规则"))
            self.assertIn("必须先检查最上方 <available_skills> 目录", system)
            self.assertIn("优先调用 skill_read", system)
            self.assertNotIn("根目录不得进入模型", system)
            self.assertIn(f"Session ID：{result.session_id}", system)
            self.assertIn("分段绝对路径：", system)
            self.assertTrue(provider.messages[-1]["content"].startswith("测试问题\n\n[本次提问时间："))

    def test_historical_tool_output_is_trimmed_only_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            memory = MemoryStore(Path(value) / ".yy" / "memory")
            session_id = memory.create_session("问题")
            memory.record_model_tool_calls(
                session_id,
                content=None,
                tool_calls=[{"id": "call_x", "type": "function", "function": {"name": "demo", "arguments": "{}"}}],
                model={}, model_call={},
            )
            raw = "A" * 6000 + "B" * 6000
            memory.record_tool_result(session_id, tool_call_id="call_x", name="demo", content=raw, status="success", arguments={})
            self.assertTrue(memory.prepare_historical_tool_outputs(
                session_id, max_chars=10000, head_ratio=0.2, tail_ratio=0.2,
            ))
            projected = memory.restore_messages(session_id)[1]["content"]
            self.assertIn("[历史工具输出已裁剪", projected)
            self.assertTrue(projected.startswith("A" * 2000))
            self.assertTrue(projected.endswith("B" * 2000))
            records = memory.session_records(session_id)
            self.assertEqual(records[-1]["content"], raw)

    def test_explicit_reasoning_is_audited_but_not_reinjected(self) -> None:
        class ReasoningProvider:
            streaming = False

            async def complete(self, messages, tools):
                return ModelReply(text="答复", reasoning="供应商显式推理")

        with tempfile.TemporaryDirectory() as value:
            config = load_runtime_config(Path(value))
            memory = MemoryStore(config.memory_dir)
            result = asyncio.run(AgentRuntime(config, provider=ReasoningProvider(), memory=memory, enable_sandbox=False).run("问题"))
            self.assertEqual(memory.session_records(result.session_id)[-1]["reasoning"], "供应商显式推理")
            self.assertNotIn("供应商显式推理", str(memory.restore_messages(result.session_id)))
