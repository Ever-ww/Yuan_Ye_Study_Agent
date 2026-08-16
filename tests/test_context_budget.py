from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from Agent import AgentRuntime, RuntimeConfig, load_runtime_config
from Agent.contracts import EventType, ModelReply, TokenUsage
from Agent.models.errors import ModelServiceError
from context_process import ContextBudgetController, ContextBudgetExceeded, ContextProcessor
from memory import MemoryStore


class _CompressionProvider:
    streaming = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return ModelReply(text=json.dumps({
            "profile_markdown": "# Profile\n稳定偏好",
            "context_summary_markdown": "# Summary\n已压缩历史",
        }, ensure_ascii=False))


class _OverflowOnceProvider:
    streaming = False

    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    async def complete(self, messages, tools):
        self.calls += 1
        self.messages = [dict(item) for item in messages]
        if self.calls == 1:
            raise ModelServiceError("maximum context length exceeded", 400)
        return ModelReply(
            text="recovered",
            usage=TokenUsage(input_tokens=1234, cached_input_tokens=1000, output_tokens=12),
        )


class _AlwaysOverflowProvider:
    streaming = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        raise ModelServiceError("prompt is too long for the context window", 400)


class _UnavailableProvider:
    streaming = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        raise ModelServiceError("compression service unavailable", 503)


def _config(root: Path, **updates) -> RuntimeConfig:
    return load_runtime_config(root, **updates)


def test_forecast_counts_output_reserve_tools_ephemeral_and_calibrates() -> None:
    with tempfile.TemporaryDirectory() as value:
        config = _config(
            Path(value),
            compression_threshold_tokens=200,
            compression_output_reserve_tokens=64,
            model_context_window_tokens=1024,
            compression_safety_margin_tokens=100,
        )
        controller = ContextBudgetController(config)
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}]
        tools = [{"name": "z", "description": "x" * 200, "parameters": {"type": "object"}}]
        estimate = controller.forecast("session", messages, tools, ephemeral_preview="q" + "e" * 200)
        assert estimate.tool_schema_tokens > 0
        assert estimate.ephemeral_context_tokens > 0
        assert estimate.projected_total_tokens == estimate.calibrated_input_tokens + 64
        calibration = controller.update_from_provider_usage(
            "session",
            estimated_input_tokens=estimate.estimated_input_tokens,
            provider_input_tokens=estimate.estimated_input_tokens * 2,
        )
        assert calibration.conservative_ratio == 2.0
        calibrated = controller.forecast("session", messages, tools)
        assert calibrated.calibrated_input_tokens >= calibrated.estimated_input_tokens * 2


def test_message_hygiene_triggers_without_provider_usage() -> None:
    with tempfile.TemporaryDirectory() as value:
        config = _config(
            Path(value),
            compression_threshold_tokens=900,
            compression_hygiene_message_limit=3,
            compression_output_reserve_tokens=0,
            model_context_window_tokens=2000,
            compression_safety_margin_tokens=100,
        )
        estimate = ContextBudgetController(config).forecast(
            "session",
            [{"role": "user", "content": str(index)} for index in range(3)],
            [],
        )
        assert estimate.decision == "compress"
        assert estimate.reason == "message_hygiene"


def test_protected_tail_is_hash_referenced_not_copied() -> None:
    with tempfile.TemporaryDirectory() as value:
        root = Path(value)
        config = _config(root, compression_threshold_tokens=2000)
        memory = MemoryStore(config.memory_dir)
        session_id = memory.create_session("old")
        memory.record_user(session_id, "old" * 1000)
        memory.record_assistant(session_id, "answer" * 1000)
        memory.record_user(session_id, "recent")
        memory.record_assistant(session_id, "recent answer")
        compressor = _CompressionProvider()
        result = asyncio.run(ContextProcessor(
            config, memory, provider_factory=lambda: compressor,
        ).compress_with_policy(session_id))
        assert result.status == "compressed"
        assert result.protected_tail_messages == 2
        active = memory.session_records(session_id)
        assert [item["role"] for item in active] == ["summary"]
        assert len(active[0]["protected_tail_refs"]) == 2
        assert [item["content"] for item in memory.restore_messages(session_id)] == [
            "recent", "recent answer",
        ]

        first_ref = active[0]["protected_tail_refs"][0]
        old_path = memory.sessions.directory / first_ref["segment"]
        old_path.write_text(old_path.read_text(encoding="utf-8").replace("recent", "tampered", 1), encoding="utf-8")
        memory.invalidate_session_cache(session_id)
        with pytest.raises(ValueError, match="Hash"):
            memory.restore_messages(session_id)


def test_hard_limit_rejects_before_provider() -> None:
    with tempfile.TemporaryDirectory() as value:
        config = _config(
            Path(value),
            model_context_window_tokens=1024,
            compression_safety_margin_tokens=128,
            compression_output_reserve_tokens=128,
            compression_threshold_tokens=800,
        )
        memory = MemoryStore(config.memory_dir)
        session_id = memory.create_session("q")
        processor = ContextProcessor(config, memory)
        with pytest.raises(ContextBudgetExceeded):
            processor.finalize_request(
                session_id,
                [{"role": "system", "content": "s" * 4000}, {"role": "user", "content": "q"}],
                [],
            )


def test_provider_context_rejection_gets_one_emergency_compression_retry() -> None:
    with tempfile.TemporaryDirectory() as value:
        root = Path(value)
        config = _config(root, compression_threshold_tokens=200000)
        memory = MemoryStore(config.memory_dir)
        session_id = memory.create_session("old")
        memory.record_user(session_id, "old question")
        memory.record_assistant(session_id, "old answer")
        provider = _OverflowOnceProvider()
        compressor = _CompressionProvider()
        runtime = AgentRuntime(
            config,
            provider=provider,
            memory=memory,
            compression_provider_factory=lambda: compressor,
            enable_sandbox=False,
        )
        events = asyncio.run(_collect(runtime, "current query", session_id))
        assert provider.calls == 2
        assert compressor.calls == 1
        assert events[-1].type is EventType.FINAL
        assert any(event.type is EventType.CONTEXT_COMPRESSED for event in events)
        assert provider.messages[-1]["role"] == "user"
        assert provider.messages[-1]["content"].startswith("<user_query>\ncurrent query")


def test_provider_context_rejection_never_retries_more_than_once() -> None:
    with tempfile.TemporaryDirectory() as value:
        root = Path(value)
        config = _config(root, compression_threshold_tokens=200000)
        memory = MemoryStore(config.memory_dir)
        session_id = memory.create_session("old")
        memory.record_user(session_id, "old question")
        memory.record_assistant(session_id, "old answer")
        provider = _AlwaysOverflowProvider()
        compressor = _CompressionProvider()
        runtime = AgentRuntime(
            config,
            provider=provider,
            memory=memory,
            compression_provider_factory=lambda: compressor,
            enable_sandbox=False,
        )
        events = asyncio.run(_collect(runtime, "current query", session_id))
        assert provider.calls == 2
        assert compressor.calls == 1
        assert events[-1].type is EventType.ERROR
        assert "最多执行一次应急压缩" in events[-1].payload["message"]


def test_unavailable_auxiliary_compressor_falls_back_to_main() -> None:
    with tempfile.TemporaryDirectory() as value:
        root = Path(value)
        config = _config(
            root,
            compression_provider="echo",
            compression_model="echo",
            compression_context_window_tokens=100000,
        )
        memory = MemoryStore(config.memory_dir)
        session_id = memory.create_session("old")
        memory.record_user(session_id, "old question")
        memory.record_assistant(session_id, "old answer")
        processor = ContextProcessor(config, memory)
        auxiliary = _UnavailableProvider()
        main = _CompressionProvider()
        processor._build_auxiliary_provider = lambda: auxiliary
        processor._build_provider = lambda: main
        result = asyncio.run(processor.compress(session_id))
        assert result.status == "compressed"
        assert auxiliary.calls == 1
        assert main.calls == 1
        assert result.compression_model.startswith("main:")
        assert result.compression_fallback_reason == "auxiliary_unavailable:ModelServiceError"


async def _collect(runtime: AgentRuntime, task: str, session_id: str):
    return [event async for event in runtime.run_task(task, session_id)]
