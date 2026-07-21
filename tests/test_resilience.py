"""CLI 网络兜底、错误快照与 Harness 空流水线测试。"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Agent import AgentRuntime, HookPoint, ModelNetworkError, ModelResponseFormatError, ModelRetryPolicy, RuntimeFailure, load_runtime_config
from Agent.contracts import ModelReply, ToolCall
from Agent.models.errors import ModelServiceError, is_retryable_model_error
from Agent.models.providers import _openai_reply
from memory import MemoryStore
from run_ui.cli import _handle_chat_failure
from run_ui.harness_loader import load_harness_module


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
        return ModelReply("成功")


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
            return ModelReply(tool_calls=(ToolCall("calculator", {"expression": "2+2"}),))
        self.second_calls += 1
        if self.second_calls <= 2:
            raise ModelNetworkError("第二步网络错误")
        return ModelReply("4")


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
                retry_policy=ModelRetryPolicy(3, 0),
            )
            points: list[str] = []

            async def observe(event) -> None:
                points.append(event.point.value)

            runtime.hooks.register(HookPoint.TURN_START, observe)
            runtime.hooks.register(HookPoint.TURN_END, observe)
            result = asyncio.run(runtime.run("只记录一次"))
            self.assertTrue(result.completed)
            self.assertEqual(provider.calls, 3)
            self.assertEqual(points.count("turn_start"), 3)
            self.assertEqual(points.count("turn_end"), 3)
            records = memory.session_records(result.session_id)
            self.assertEqual([record["role"] for record in records], ["user", "assistant"])

    def test_retry_budget_resets_after_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            provider = PerStepFlakyProvider()
            runtime = AgentRuntime(
                load_runtime_config(Path(value)),
                provider=provider,
                retry_policy=ModelRetryPolicy(3, 0),
            )
            result = asyncio.run(runtime.run("计算"))
            self.assertTrue(result.completed)
            self.assertEqual(provider.first_calls, 2)
            self.assertEqual(provider.second_calls, 3)

    def test_retry_exhaustion_preserves_failure_context(self) -> None:
        async def run_case(root: Path):
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=FlakyProvider(3),
                retry_policy=ModelRetryPolicy(3, 0),
                raise_errors=True,
            )
            with self.assertRaises(ModelNetworkError):
                await runtime.run("失败问题")
            return runtime

        with tempfile.TemporaryDirectory() as value:
            runtime = asyncio.run(run_case(Path(value)))
            self.assertIsNotNone(runtime.last_failure)
            self.assertEqual(runtime.last_failure.category, "network")
            self.assertEqual(len(runtime.last_failure.retry_history), 3)
            self.assertEqual(runtime.last_failure.messages[-1]["content"], "失败问题")

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
            self.assertIn("session_record", kinds)
            self.assertIn("message", kinds)
            self.assertIn("tool_schema", kinds)
            self.assertIn("error", kinds)

    def test_declined_repair_records_decision_without_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            memory = MemoryStore(config.memory_dir)
            session_id = memory.create_session("问题")
            memory.record_user(session_id, "问题")
            runtime = AgentRuntime(config, provider=FlakyProvider(0), memory=memory)
            failure = RuntimeFailure.capture(ModelResponseFormatError("格式错误", "{}"))
            with patch("run_ui.cli.typer.confirm", return_value=False):
                asyncio.run(_handle_chat_failure(config, runtime, "问题", session_id, failure))
            snapshots = list((root / "tests" / "error").glob("*.jsonl"))
            self.assertEqual(len(snapshots), 1)
            self.assertIn('"confirmed": false', snapshots[0].read_text(encoding="utf-8"))
            self.assertFalse((root / ".yy" / "harness-evolution" / "worktrees").exists())

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
            request = harness.HarnessEvolutionRequest(root, snapshot.stem, snapshot, "问题", config)
            result = asyncio.run(harness.HarnessEvolutionRunner(writer).run(request))
            self.assertEqual(result.status, "dirty_worktree")
            self.assertFalse((root / ".yy" / "harness-evolution" / "worktrees").exists())

    def test_coding_runtime_reuses_agent_class_without_tools_or_memory_files(self) -> None:
        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root)
            worktree = root / "isolated"
            worktree.mkdir()
            runtime = harness.create_coding_runtime(config, worktree)
            self.assertIsInstance(runtime, AgentRuntime)
            self.assertEqual(runtime.tools.schemas(), [])
            result = asyncio.run(runtime.run("只诊断"))
            self.assertTrue(result.completed)
            self.assertFalse((worktree / ".yy").exists())

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
            request = harness.HarnessEvolutionRequest(root, snapshot.stem, snapshot, "问题", config)
            result = asyncio.run(harness.HarnessEvolutionRunner(writer).run(request))
            self.assertEqual(result.status, "no_code_changes")
            self.assertFalse(Path(result.worktree_path).exists())
            self.assertNotIn(result.branch, _git(root, "branch", "--list"))
            text = snapshot.read_text(encoding="utf-8")
            self.assertIn('"status": "no_code_changes"', text)
            self.assertIn('"status": "cleanup"', text)

    def test_injected_future_capability_can_test_and_merge(self) -> None:
        harness = load_harness_module()

        class EditingRuntime:
            def __init__(self, worktree: Path) -> None:
                self.worktree = worktree

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
            request = harness.HarnessEvolutionRequest(root, snapshot.stem, snapshot, "问题", config)
            runner = PassingRunner(writer, runtime_factory=lambda current, worktree: EditingRuntime(worktree))
            result = asyncio.run(runner.run(request))
            self.assertTrue(result.merged)
            self.assertEqual((root / "tracked.txt").read_text(encoding="utf-8"), "fixed\n")
            self.assertFalse(Path(result.worktree_path).exists())

    def test_injected_failed_tests_discard_worktree_and_branch(self) -> None:
        harness = load_harness_module()

        class EditingRuntime:
            def __init__(self, worktree: Path) -> None:
                self.worktree = worktree

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
            request = harness.HarnessEvolutionRequest(root, snapshot.stem, snapshot, "问题", config)
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
