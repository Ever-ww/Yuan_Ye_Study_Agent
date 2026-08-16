"""CLI 网络兜底、错误快照与 Harness Coding 流水线测试。"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Agent import AgentRuntime, EventType, HookPoint, ModelNetworkError, ModelResponseFormatError, ModelRetryPolicy, RuntimeFailure, load_runtime_config
from Agent.contracts import ModelReply, ToolCall
from Agent.models.errors import ModelServiceError, is_retryable_model_error
from Agent.models.providers import _openai_reply
from memory import MemoryStore
from run_ui.cli import _handle_chat_failure
from run_ui.harness_loader import load_harness_module
from tool import AsyncToolRegistry


class FlakyProvider:
    """失败指定次数后成功的网络重试测试 Provider。"""

    streaming = False

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls <= self.failures:
            raise ModelNetworkError(f"临时网络错误 {self.calls}")
        return ModelReply(text="成功")


class HangingProvider:
    """模拟等待网络响应、只能通过任务取消终止的 Provider。"""

    streaming = False

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(self, messages, tools):
        del messages, tools
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("不可到达")


class PartialStreamingProvider:
    """产生文本和未完成工具调用后保持连接，等待 Ctrl+C。"""

    streaming = True

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(self, messages, tools):
        del messages, tools
        yield ModelReply(
            text="这是已经生成的一半内容",
            tool_calls=(ToolCall(name="calculator", arguments={"expression": "2+"}),),
            finished=False,
        )
        self.started.set()
        await asyncio.Event().wait()


class CapturingProvider:
    streaming = False

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def complete(self, messages, tools):
        del tools
        self.messages = [dict(message) for message in messages]
        return ModelReply(text="已继续")


class ToolRequestProvider:
    """立即请求一个会挂起的工具。"""

    streaming = False

    async def complete(self, messages, tools):
        del messages, tools
        return ModelReply(tool_calls=(ToolCall(name="hanging_tool", arguments={}),))


class HangingTool:
    name = "hanging_tool"
    description = "等待取消的测试工具"
    schema = {"type": "object", "properties": {}}
    risk = "read"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, arguments, context):
        del arguments, context
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("不可到达")


class _HarnessCheckpoint:
    commit_sha = "0" * 40

    def model_dump(self, mode="python"):
        del mode
        return {"commit_sha": self.commit_sha}


class _FakeHarnessSandbox:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.closed = 0
        self.writes: list[str] = []

    async def start(self, session_id: str):
        self.started.append(session_id)
        return _HarnessCheckpoint()

    async def close(self):
        self.closed += 1

    async def checkpoint_write(self, path: str):
        self.writes.append(path)
        return _HarnessCheckpoint()

    async def restore_current(self):
        return None


class _FailingHarnessSandbox(_FakeHarnessSandbox):
    async def start(self, session_id: str):
        del session_id
        raise RuntimeError("Docker unavailable")


class _CodingWriteProvider:
    streaming = False

    async def complete(self, messages, tools):
        del tools
        if not any(message.get("role") == "tool" for message in messages):
            return ModelReply(tool_calls=(ToolCall(
                name="write",
                arguments={"path": "fixed.py", "content": "FIXED = True\n"},
            ),))
        return ModelReply(text="已完成隔离修复")


class _HarnessMemoryProvider:
    """返回可校验的 Harness 长期记忆更新。"""

    streaming = False

    async def complete(self, messages, tools):
        del messages, tools
        return ModelReply(text=json.dumps({
            "project_markdown": "# Curated Project\n\n- 当前架构已经更新。",
            "change_entry_markdown": "## 2026-07-28 - 已验证修复\n\n- 测试全部通过。",
            "lesson_entry_markdown": "## 2026-07-28 - 可复用经验\n\n- 合并前再次检查主分支。",
        }, ensure_ascii=False))


class PerStepFlakyProvider:
    """两次待完成调用分别失败后成功，用于验证计数重置。"""

    streaming = False

    def __init__(self) -> None:
        self.first_calls = 0
        self.second_calls = 0

    async def complete(self, messages, tools):
        if not any(message.get("role") == "tool" for message in messages):
            self.first_calls += 1
            if self.first_calls == 1:
                raise ModelNetworkError("第一步网络错误")
            return ModelReply(tool_calls=(ToolCall(name="calculator", arguments={"expression": "2+2"}),))
        self.second_calls += 1
        if self.second_calls <= 2:
            raise ModelNetworkError("第二步网络错误")
        return ModelReply(text="4")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    (root / ".gitignore").write_text(".yy/\ntests/error/*.jsonl\n", encoding="utf-8")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "tracked.txt")
    _git(
        root,
        "-c", "user.name=Harness Test",
        "-c", "user.email=harness-test@local.invalid",
        "commit", "-m", "initial",
    )


class ResilienceTests(unittest.TestCase):
    def test_network_retries_are_independent_turns_and_user_is_recorded_once(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            provider = FlakyProvider(2)
            runtime = AgentRuntime(
                config,
                provider=provider,
                memory=memory,
                retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=0),
                enable_sandbox=False,
            )
            points: list[str] = []

            async def observe(event) -> None:
                points.append(event.point.value)

            runtime.hooks.register(HookPoint.TURN_START, observe)
            runtime.hooks.register(HookPoint.TURN_END, observe)
            result = asyncio.run(runtime.run("只记录一次"))
            self.assertTrue(result.completed)
            self.assertEqual(provider.calls, 3)
            self.assertEqual(points.count("turn_start"), 1)
            self.assertEqual(points.count("turn_end"), 1)
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant"])

    def test_network_recovery_emits_reconnected_event(self) -> None:
        async def check(root: Path) -> tuple[list[EventType], int]:
            provider = FlakyProvider(2)
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=provider,
                retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=0),
                enable_sandbox=False,
            )
            try:
                events = [event.type async for event in runtime.run_task("等待重连")]
                return events, provider.calls
            finally:
                await runtime.close()

        with tempfile.TemporaryDirectory() as value:
            events, calls = asyncio.run(check(Path(value)))
            self.assertEqual(calls, 3)
            self.assertEqual(events.count(EventType.MODEL_RETRY), 2)
            self.assertEqual(events.count(EventType.MODEL_RECONNECTED), 1)
            self.assertEqual(events[-1], EventType.FINAL)

    def test_cancelling_current_answer_keeps_session_recoverable(self) -> None:
        async def check(root: Path) -> list[dict[str, object]]:
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            provider = HangingProvider()
            runtime = AgentRuntime(
                config,
                provider=provider,
                memory=memory,
                retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=0),
                enable_sandbox=False,
            )

            async def consume() -> None:
                async for _ in runtime.run_task("会被取消的问题"):
                    pass

            running = asyncio.create_task(consume())
            await provider.started.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            session_id = str(runtime.active_session_id)
            runtime.provider = FlakyProvider(0)
            followup = [event async for event in runtime.run_task("取消后的新问题", session_id)]
            self.assertEqual(followup[-1].type, EventType.FINAL)
            records = memory.session_records(session_id)
            await runtime.close()
            return records

        with tempfile.TemporaryDirectory() as value:
            records = asyncio.run(check(Path(value)))
            self.assertEqual([record["role"] for record in records], [
                "user", "assistant", "user", "assistant",
            ])
            self.assertEqual(records[1]["status"], "cancelled")
            self.assertIn("Ctrl+C", records[1]["content"])

    def test_streaming_cancellation_keeps_partial_text_without_tool_calls(self) -> None:
        async def check(root: Path):
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            provider = PartialStreamingProvider()
            runtime = AgentRuntime(
                config,
                provider=provider,
                memory=memory,
                enable_sandbox=False,
            )

            async def consume() -> None:
                async for _ in runtime.run_task("保留部分回答"):
                    pass

            running = asyncio.create_task(consume())
            await provider.started.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            session_id = str(runtime.active_session_id)
            cancelled_records = memory.session_records(session_id)

            capturing = CapturingProvider()
            runtime.provider = capturing
            followup = [event async for event in runtime.run_task("继续回答", session_id)]
            await runtime.close()
            return cancelled_records, capturing.messages, followup

        with tempfile.TemporaryDirectory() as value:
            records, next_messages, followup = asyncio.run(check(Path(value)))
            self.assertEqual([record["role"] for record in records], ["user", "assistant"])
            self.assertEqual(records[-1]["content"], "这是已经生成的一半内容")
            self.assertEqual(records[-1]["status"], "cancelled")
            self.assertNotIn("tool_calls", records[-1])
            partial = next(
                message for message in next_messages
                if message.get("role") == "assistant"
                and message.get("content") == "这是已经生成的一半内容"
            )
            self.assertNotIn("tool_calls", partial)
            self.assertEqual(followup[-1].type, EventType.FINAL)

    def test_cancelling_running_tool_closes_tool_chain(self) -> None:
        async def check(root: Path) -> list[dict[str, object]]:
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            tool = HangingTool()
            runtime = AgentRuntime(
                config,
                provider=ToolRequestProvider(),
                memory=memory,
                tools=AsyncToolRegistry([tool]),
                enable_sandbox=False,
            )

            async def consume() -> None:
                async for _ in runtime.run_task("调用会挂起的工具"):
                    pass

            running = asyncio.create_task(consume())
            await tool.started.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            records = memory.session_records(str(runtime.active_session_id))
            await runtime.close()
            return records

        with tempfile.TemporaryDirectory() as value:
            records = asyncio.run(check(Path(value)))
            self.assertEqual(
                [record["role"] for record in records],
                ["user", "assistant", "tool", "assistant"],
            )
            self.assertEqual(records[2]["status"], "cancelled")
            self.assertEqual(records[3]["status"], "cancelled")
            self.assertEqual(records[1]["tool_calls"][0]["id"], records[2]["tool_call_id"])

    def test_retry_budget_resets_after_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            provider = PerStepFlakyProvider()
            runtime = AgentRuntime(
                load_runtime_config(Path(value)),
                provider=provider,
                retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=0),
                enable_sandbox=False,
            )
            result = asyncio.run(runtime.run("计算"))
            self.assertTrue(result.completed)
            self.assertEqual(provider.first_calls, 2)
            self.assertEqual(provider.second_calls, 3)

    def test_retry_exhaustion_preserves_failure_context(self) -> None:
        async def run_case(root: Path):
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            runtime = AgentRuntime(
                config,
                provider=FlakyProvider(3),
                memory=memory,
                retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=0),
                raise_errors=True,
                enable_sandbox=False,
            )
            with self.assertRaises(ModelNetworkError):
                async for _ in runtime.run_task("失败问题"):
                    pass
            failure = runtime.last_failure
            session_id = str(runtime.active_session_id)
            failed_records = memory.session_records(session_id)
            runtime.provider = FlakyProvider(0)
            followup = [event async for event in runtime.run_task("网络恢复后的问题", session_id)]
            all_records = memory.session_records(session_id)
            await runtime.close()
            return failure, failed_records, followup, all_records

        with tempfile.TemporaryDirectory() as value:
            failure, failed_records, followup, all_records = asyncio.run(run_case(Path(value)))
            self.assertIsNotNone(failure)
            self.assertEqual(failure.category, "network")
            self.assertEqual(len(failure.retry_history), 3)
            self.assertTrue(failure.messages[-1]["content"].startswith("失败问题\n\n[本次提问时间："))
            self.assertEqual([record["role"] for record in failed_records], ["user", "assistant"])
            self.assertEqual(failed_records[-1]["status"], "network_error")
            self.assertEqual(followup[-1].type, EventType.FINAL)
            self.assertEqual(
                [record["role"] for record in all_records],
                ["user", "assistant", "user", "assistant"],
            )

    def test_http_retry_classification(self) -> None:
        self.assertTrue(is_retryable_model_error(ModelServiceError("busy", 429)))
        self.assertTrue(is_retryable_model_error(ModelServiceError("down", 503)))
        self.assertFalse(is_retryable_model_error(ModelServiceError("bad key", 401)))
        self.assertFalse(is_retryable_model_error(ModelServiceError("bad request", 400)))

    def test_openai_response_normalization(self) -> None:
        reply = _openai_reply({
            "choices": [{"message": {
                "content": [{"type": "text", "text": "先"}, {"text": "后"}],
                "tool_calls": [{
                    "id": "call_x",
                    "function": {"name": "calculator", "arguments": {"expression": "2+2"}},
                }],
            }}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        })
        self.assertEqual(reply.text, "先后")
        self.assertEqual(reply.tool_calls[0].arguments, {"expression": "2+2"})
        with self.assertRaises(ModelResponseFormatError):
            _openai_reply({"unexpected": True})

    def test_snapshot_is_complete_hash_named_and_redacted(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            memory = MemoryStore(root / ".yy" / "memory")
            session_id = memory.create_session("问题")
            memory.record_user(session_id, "问题")
            error = ModelResponseFormatError("格式错误", "secret-key response")
            setattr(error, "yy_failure_context", {
                "messages": [{"role": "system", "content": "secret-key 规则"}, {"role": "user", "content": "问题"}],
                "tools": [{"name": "calculator", "parameters": {"type": "object"}}],
                "model": {"provider": "openai", "name": "demo", "base_url": "https://example.test/v1"},
                "retry_history": [],
            })
            failure = RuntimeFailure.capture(error)
            writer = harness.ErrorSnapshotWriter(root, secrets=("secret-key",))
            path = writer.capture(
                task="问题",
                session_id=session_id,
                failure=failure,
                session_records=memory.session_records(session_id),
            )
            self.assertRegex(path.name, r"^[0-9a-f]{64}\.jsonl$")
            self.assertFalse((path.parent / "index.json").exists())
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-key", text)
            records = [json.loads(line) for line in text.splitlines()]
            kinds = [record["record_type"] for record in records]
            self.assertIn("incident", kinds)
            self.assertIn("session_audit", kinds)
            self.assertIn("message", kinds)
            self.assertIn("tool_schema", kinds)
            self.assertIn("error", kinds)
            self.assertNotIn("session_record", kinds)
            self.assertTrue(all("content" not in record for record in records if record["record_type"] == "session_audit"))
            self.assertEqual(
                sum(record.get("content") == "问题" for record in records),
                1,
            )

    def test_local_cli_does_not_execute_or_persist_error_evolution(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            session_id = memory.create_session("问题")
            memory.record_user(session_id, "问题")
            runtime = AgentRuntime(
                config, provider=FlakyProvider(0), memory=memory, enable_sandbox=False,
            )
            failure = RuntimeFailure.capture(ModelResponseFormatError("格式错误", "{}"))
            with patch("run_ui.cli.typer.confirm", return_value=False):
                asyncio.run(_handle_chat_failure(config, runtime, "问题", session_id, failure))
            snapshots = list((root / "tests" / "error").glob("*.jsonl"))
            self.assertEqual(snapshots, [])
            self.assertFalse((root / ".yy" / "harness-evolution" / "worktrees").exists())

    def test_network_failure_does_not_create_snapshot(self) -> None:
        """网络重试耗尽只向 CLI 报错，不应产生代码缺陷快照。"""
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            session_id = memory.create_session("网络失败问题")
            memory.record_user(session_id, "网络失败问题")
            runtime = AgentRuntime(
                config, provider=FlakyProvider(0), memory=memory, enable_sandbox=False,
            )
            failure = RuntimeFailure.capture(ModelNetworkError("连接失败"))

            asyncio.run(_handle_chat_failure(config, runtime, "网络失败问题", session_id, failure))

            self.assertFalse((root / "tests" / "error").exists())

    def test_service_failure_does_not_create_snapshot(self) -> None:
        """模型服务状态错误同样不属于可通过 Harness 修复的代码缺陷。"""
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            runtime = AgentRuntime(config, provider=FlakyProvider(0), enable_sandbox=False)
            failure = RuntimeFailure.capture(ModelServiceError("服务不可用", 503))

            asyncio.run(_handle_chat_failure(config, runtime, "服务失败问题", "", failure))

            self.assertFalse((root / "tests" / "error").exists())

    def test_dirty_worktree_stops_harness(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            _init_repo(root)
            config = load_runtime_config(root)
            failure = RuntimeFailure.capture(RuntimeError("内部缺陷"))
            writer = harness.ErrorSnapshotWriter(root)
            snapshot = writer.capture(task="问题", session_id="a" * 16, failure=failure, session_records=[])
            (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            request = harness.HarnessEvolutionRequest(
                project_root=root, incident_id=snapshot.stem, snapshot_path=snapshot, task="问题", config=config,
            )
            runner = harness.HarnessEvolutionRunner(
                writer,
                runtime_factory=lambda current, worktree: harness.create_coding_runtime(
                    current,
                    worktree,
                    sandbox=_FakeHarnessSandbox(),
                ),
            )
            result = asyncio.run(runner.run(request))
            self.assertEqual(result.status, "dirty_worktree")
            self.assertFalse((root / ".yy" / "harness-evolution" / "worktrees").exists())

    def test_coding_runtime_has_persistent_memory_tools_subagent_and_sandbox(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(
                root,
                coding_source_root=root,
                web_search_api_key="configured-test-key",
            )
            worktree = root / "isolated"
            worktree.mkdir(parents=True)
            source = root / "skills" / "coding-helper"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\n"
                "name: coding-helper\n"
                "description: 帮助诊断代码缺陷\n"
                "license: MIT\n"
                "---\n\n"
                "先定位错误边界，再提出最小修复。\n",
                encoding="utf-8",
            )
            (worktree / "module.py").write_text("BROKEN = True\n", encoding="utf-8")
            sandbox = _FakeHarnessSandbox()
            runtime = harness.create_coding_runtime(config, worktree, sandbox=sandbox)
            runtime.provider = _CodingWriteProvider()
            self.assertIsInstance(runtime, AgentRuntime)
            schemas = runtime.tools.schemas()
            names = {schema["name"] for schema in schemas}
            self.assertEqual(names, {
                "read_file",
                "edit",
                "write",
                "search_workspace",
                "bash",
                "sandbox_rollback",
                "web_search",
                "web_fetch",
                "skill_read",
                "subagent",
            })
            self.assertTrue({
                "calculator",
                "current_time",
                "download_paper",
                "reference_search",
                "reference_get",
                "reference_write",
                "cronjob",
                "skill_install",
            }.isdisjoint(names))
            subagent_schema = next(
                schema["parameters"] for schema in schemas if schema["name"] == "subagent"
            )
            self.assertEqual(
                set(subagent_schema["properties"]["tools"]["items"]["enum"]),
                names - {"subagent"},
            )
            self.assertIsNotNone(runtime.skills)
            self.assertEqual(runtime.skills.source_root, root.resolve())
            self.assertEqual(runtime.skills.skills_root, (root / "skills").resolve())
            self.assertIsNone(runtime.references)
            self.assertIsNotNone(runtime.context_processor)
            self.assertIn("coding-helper", runtime.prompts.compose("诊断")[0]["content"])
            runtime.skills.bind_session(
                runtime.coding_session_id,
                runtime.skills.catalog_snapshot(),
            )
            skill_text = asyncio.run(runtime.tools.execute(
                "skill_read",
                {"name": "coding-helper"},
                runtime.tool_context.model_copy(update={"session_id": runtime.coding_session_id}),
            ))
            self.assertIn("最小修复", skill_text)
            self.assertEqual(runtime.config.workspace_root, worktree.resolve())
            self.assertEqual(runtime.tool_context.project_root, worktree.resolve())
            expected_memory = root / ".yy" / "harness-evolution" / "memory"
            self.assertEqual(runtime.memory.root, expected_memory.resolve())
            self.assertEqual(runtime.memory.sessions.directory, expected_memory / "session")
            self.assertIn("module.py", runtime.memory.profile_context(runtime.coding_session_id))
            result = asyncio.run(runtime.run("修复 module.py", runtime.coding_session_id))
            self.assertTrue(result.completed)
            self.assertEqual((worktree / "fixed.py").read_text(encoding="utf-8"), "FIXED = True\n")
            self.assertEqual(sandbox.started, [runtime.coding_session_id])
            self.assertEqual(sandbox.writes, ["fixed.py"])
            self.assertGreaterEqual(sandbox.closed, 1)
            records = runtime.memory.session_records(runtime.coding_session_id)
            self.assertEqual([record["role"] for record in records[-4:]], [
                "user",
                "assistant",
                "tool",
                "assistant",
            ])
            self.assertFalse((worktree / ".yy").exists())

            second_worktree = root / "isolated-second"
            second_worktree.mkdir()
            second = harness.create_coding_runtime(
                config,
                second_worktree,
                sandbox=_FakeHarnessSandbox(),
            )
            self.assertNotEqual(second.coding_session_id, runtime.coding_session_id)
            self.assertTrue(second.memory.has_session(second.coding_session_id))
            self.assertEqual(second.memory.session_records(second.coding_session_id), [])
            profile_files = {
                path.name
                for path in (expected_memory / "profile").glob("*.md")
            }
            self.assertEqual(profile_files, {
                "AGENT.md",
                "PROJECT.md",
                "CHANGES.md",
                "LESSONS.md",
            })
            self.assertNotIn(runtime.coding_session_id + ".md", profile_files)

            agent_path = expected_memory / "profile" / "AGENT.md"
            agent_path.write_text("# 用户维护的规则\n保持只读\n", encoding="utf-8")
            third_worktree = root / "isolated-third"
            third_worktree.mkdir()
            harness.create_coding_runtime(
                config,
                third_worktree,
                sandbox=_FakeHarnessSandbox(),
            )
            self.assertEqual(
                agent_path.read_text(encoding="utf-8"),
                "# 用户维护的规则\n保持只读\n",
            )

    def test_coding_runtime_always_fetches_but_only_searches_with_key(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            worktree = root / "isolated"
            worktree.mkdir()
            runtime = harness.create_coding_runtime(
                load_runtime_config(root),
                worktree,
                sandbox=_FakeHarnessSandbox(),
            )
            names = set(runtime.tools.names())
            self.assertIn("web_fetch", names)
            self.assertNotIn("web_search", names)

    def test_forbidden_future_changes_are_rejected(self) -> None:
        harness = load_harness_module()
        status = "?? .yy/settings.local.json\n M Agent/runtime/engine.py\n?? .env.local\n"
        self.assertEqual(
            harness._forbidden_changed_paths(status),
            [".yy/settings.local.json", ".env.local"],
        )

    def test_empty_coding_runtime_cleans_worktree_without_tests(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            _init_repo(root)
            config = load_runtime_config(root)
            failure = RuntimeFailure.capture(RuntimeError("内部缺陷"))
            writer = harness.ErrorSnapshotWriter(root)
            snapshot = writer.capture(task="问题", session_id="b" * 16, failure=failure, session_records=[])
            request = harness.HarnessEvolutionRequest(
                project_root=root, incident_id=snapshot.stem, snapshot_path=snapshot, task="问题", config=config,
            )
            runner = harness.HarnessEvolutionRunner(
                writer,
                runtime_factory=lambda current, worktree: harness.create_coding_runtime(
                    current,
                    worktree,
                    sandbox=_FakeHarnessSandbox(),
                ),
            )
            result = asyncio.run(runner.run(request))
            self.assertEqual(result.status, "no_code_changes")
            self.assertFalse(Path(result.worktree_path).exists())
            self.assertNotIn(result.branch, _git(root, "branch", "--list"))
            text = snapshot.read_text(encoding="utf-8")
            self.assertIn('"status": "no_code_changes"', text)
            self.assertIn('"status": "cleanup"', text)

    def test_harness_rejects_runtime_outside_isolated_worktree(self) -> None:
        harness = load_harness_module()

        class WrongWorkspaceRuntime:
            def __init__(self, workspace_root: Path) -> None:
                self.workspace_root = workspace_root

            async def run(self, task):
                raise AssertionError(f"不应运行：{task}")

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            _init_repo(root)
            config = load_runtime_config(root)
            failure = RuntimeFailure.capture(RuntimeError("内部缺陷"))
            writer = harness.ErrorSnapshotWriter(root)
            snapshot = writer.capture(
                task="问题",
                session_id="e" * 16,
                failure=failure,
                session_records=[],
            )
            request = harness.HarnessEvolutionRequest(
                project_root=root,
                incident_id=snapshot.stem,
                snapshot_path=snapshot,
                task="问题",
                config=config,
            )
            runner = harness.HarnessEvolutionRunner(
                writer,
                runtime_factory=lambda current, worktree: WrongWorkspaceRuntime(root),
            )

            result = asyncio.run(runner.run(request))

            self.assertEqual(result.status, "invalid_runtime_workspace")
            self.assertFalse(Path(result.worktree_path).exists())
            self.assertNotIn(result.branch, _git(root, "branch", "--list"))
            self.assertIn('"status": "invalid_runtime_workspace"', snapshot.read_text(encoding="utf-8"))

    def test_harness_reports_coding_runtime_failure_instead_of_no_changes(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            _init_repo(root)
            config = load_runtime_config(root)
            failure = RuntimeFailure.capture(RuntimeError("内部缺陷"))
            writer = harness.ErrorSnapshotWriter(root)
            snapshot = writer.capture(
                task="问题",
                session_id="f" * 16,
                failure=failure,
                session_records=[],
            )
            request = harness.HarnessEvolutionRequest(
                project_root=root,
                incident_id=snapshot.stem,
                snapshot_path=snapshot,
                task="问题",
                config=config,
            )
            runner = harness.HarnessEvolutionRunner(
                writer,
                runtime_factory=lambda current, worktree: harness.create_coding_runtime(
                    current,
                    worktree,
                    sandbox=_FailingHarnessSandbox(),
                ),
            )
            result = asyncio.run(runner.run(request))
            self.assertEqual(result.status, "coding_runtime_failed")
            self.assertIn("Docker unavailable", result.message)
            self.assertFalse(Path(result.worktree_path).exists())
            self.assertIn('"status": "coding_runtime_failed"', snapshot.read_text(encoding="utf-8"))

    def test_injected_future_capability_can_test_and_merge(self) -> None:
        harness = load_harness_module()

        class EditingRuntime:
            def __init__(self, worktree: Path) -> None:
                self.worktree = worktree
                self.workspace_root = worktree

            async def run(self, task):
                del task
                (self.worktree / "tracked.txt").write_text("fixed\n", encoding="utf-8")
                return type("Result", (), {"answer": "已生成修复"})()

            async def close(self):
                return None

        class PassingRunner(harness.HarnessEvolutionRunner):
            async def _run_tests(self, worktree, snapshot_path):
                self.writer.append_event(snapshot_path, "test", command=["injected"], returncode=0)
                return True

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            _init_repo(root)
            config = load_runtime_config(root)
            failure = RuntimeFailure.capture(RuntimeError("内部缺陷"))
            writer = harness.ErrorSnapshotWriter(root)
            snapshot = writer.capture(task="问题", session_id="c" * 16, failure=failure, session_records=[])
            request = harness.HarnessEvolutionRequest(
                project_root=root, incident_id=snapshot.stem, snapshot_path=snapshot, task="问题", config=config,
            )
            runner = PassingRunner(writer, runtime_factory=lambda current, worktree: EditingRuntime(worktree))
            result = asyncio.run(runner.run(request))
            self.assertTrue(result.merged)
            self.assertEqual((root / "tracked.txt").read_text(encoding="utf-8"), "fixed\n")
            self.assertFalse(Path(result.worktree_path).exists())
            profile = root / ".yy" / "harness-evolution" / "memory" / "profile"
            self.assertIn("问题", (profile / "CHANGES.md").read_text(encoding="utf-8"))
            self.assertIn(
                "Project Architecture",
                (profile / "PROJECT.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                {path.name for path in profile.glob("*.md")},
                {"AGENT.md", "PROJECT.md", "CHANGES.md", "LESSONS.md"},
            )

    def test_successful_memory_curator_updates_only_the_three_managed_files(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            _init_repo(root)
            config = load_runtime_config(root)
            writer = harness.ErrorSnapshotWriter(root)
            failure = RuntimeFailure.capture(RuntimeError("内部缺陷"))
            snapshot = writer.capture(
                task="修复记忆更新",
                session_id="9" * 16,
                failure=failure,
                session_records=[],
            )
            request = harness.HarnessEvolutionRequest(
                project_root=root,
                incident_id=snapshot.stem,
                snapshot_path=snapshot,
                task="修复记忆更新",
                config=config,
            )
            runner = harness.HarnessEvolutionRunner(
                writer,
                memory_provider_factory=lambda current: _HarnessMemoryProvider(),
            )
            profile = root / ".yy" / "harness-evolution" / "memory" / "profile"
            profile.mkdir(parents=True)
            agent = profile / "AGENT.md"
            agent.write_text("# 用户规则\n不得覆盖\n", encoding="utf-8")
            asyncio.run(runner._update_long_term_memory(
                request,
                root,
                diagnostic="已完成",
                commit_sha="a" * 40,
                changed_files=["tracked.txt"],
            ))
            self.assertEqual(agent.read_text(encoding="utf-8"), "# 用户规则\n不得覆盖\n")
            self.assertIn("Curated Project", (profile / "PROJECT.md").read_text(encoding="utf-8"))
            self.assertIn("已验证修复", (profile / "CHANGES.md").read_text(encoding="utf-8"))
            self.assertIn("可复用经验", (profile / "LESSONS.md").read_text(encoding="utf-8"))

    def test_injected_failed_tests_discard_worktree_and_branch(self) -> None:
        harness = load_harness_module()

        class EditingRuntime:
            def __init__(self, worktree: Path) -> None:
                self.worktree = worktree
                self.workspace_root = worktree

            async def run(self, task):
                del task
                (self.worktree / "tracked.txt").write_text("broken\n", encoding="utf-8")
                return type("Result", (), {"answer": "尝试修复"})()

            async def close(self):
                return None

        class FailingRunner(harness.HarnessEvolutionRunner):
            async def _run_tests(self, worktree, snapshot_path):
                self.writer.append_event(snapshot_path, "test", command=["injected"], returncode=1)
                return False

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            _init_repo(root)
            config = load_runtime_config(root)
            failure = RuntimeFailure.capture(RuntimeError("内部缺陷"))
            writer = harness.ErrorSnapshotWriter(root)
            snapshot = writer.capture(task="问题", session_id="d" * 16, failure=failure, session_records=[])
            request = harness.HarnessEvolutionRequest(
                project_root=root, incident_id=snapshot.stem, snapshot_path=snapshot, task="问题", config=config,
            )
            runner = FailingRunner(writer, runtime_factory=lambda current, worktree: EditingRuntime(worktree))
            result = asyncio.run(runner.run(request))
            self.assertEqual(result.status, "tests_failed")
            self.assertEqual((root / "tracked.txt").read_text(encoding="utf-8"), "base\n")
            self.assertFalse(Path(result.worktree_path).exists())
            self.assertNotIn(result.branch, _git(root, "branch", "--list"))

    def test_repository_ignores_runtime_error_snapshots(self) -> None:
        ignore = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("tests/error/*.jsonl", ignore)
        self.assertTrue(re.search(r"tests/error/\*\.jsonl", ignore))


if __name__ == "__main__":
    unittest.main()
