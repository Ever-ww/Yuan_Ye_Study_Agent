"""Request-history cache invariants, independently of provider cache telemetry."""
import asyncio
import copy
import json
from datetime import datetime

import pytest

from Agent import AgentRuntime, load_runtime_config
from Agent.contracts import ModelReply, ToolCall
from Agent.models.errors import ModelServiceError
from context_process import ContextProcessor
from memory import MemoryStore
from memory.persistence import SessionPersistenceProjection
from memory.provider_context import ProviderContextRecord, context_baseline
from prompt.runtime_context import AgentDynamicContextBuilder
from tool import AsyncToolRegistry
from tools.calculator import CalculatorTool


class Capture:
    streaming = False

    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append(copy.deepcopy(messages))
        return ModelReply(text="done")


def runtime(config, memory, provider, **kwargs):
    return AgentRuntime(config, memory=memory, provider=provider,
                        enable_sandbox=False, enable_subagent=False,
                        enable_references=False, enable_paper_library=False, **kwargs)


def config_for(path):
    return load_runtime_config(path, compression_threshold_tokens=0, memory_retrieval_enabled=False)


def test_same_day_changes_only_and_restart_exact_prefix(tmp_path, monkeypatch):
    class Clock(datetime):
        day = 10
        hour = 1

        @classmethod
        def now(cls):
            return datetime(2026, 9, cls.day, cls.hour).astimezone()

    monkeypatch.setattr("prompt.runtime_context.datetime", Clock)
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    provider = Capture()
    first = runtime(config, memory, provider, tools=AsyncToolRegistry())
    assert asyncio.run(first.run("one", sid)).completed
    original = memory.active_path(sid).read_bytes()
    Clock.hour = 2
    # A fresh Store and Runtime may not rely on a process-local cache baseline.
    memory = MemoryStore(config.memory_dir)
    second = runtime(config, memory, provider, tools=AsyncToolRegistry())
    assert asyncio.run(second.run("two", sid)).completed
    assert provider.calls[1][:len(provider.calls[0])] == provider.calls[0]
    assert provider.calls[1][-1]["content"] == "two"
    assert memory.active_path(sid).read_bytes().startswith(original)
    Clock.hour = 4
    assert asyncio.run(second.run("three", sid)).completed
    assert provider.calls[2][:len(provider.calls[1])] == provider.calls[1]
    assert '"current_time":"2026-09-10T04:00:00' in provider.calls[2][-1]["content"]
    users = [r for r in memory.session_records(sid) if r["role"] == "user"]
    assert users[1]["provider_context"]["fragments"] == {}
    assert users[0]["provider_context"]["fragments"]["agent"] != users[2]["provider_context"]["fragments"]["agent"]


def test_network_failure_persists_exact_projection_before_io(tmp_path):
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")

    class Offline(Capture):
        async def complete(self, messages, tools):
            self.calls.append(copy.deepcopy(messages))
            # Re-open independently while provider I/O is about to fail.
            recovered = MemoryStore(config.memory_dir).restore_messages(sid, provider_context=True)
            assert recovered[0] == messages[-1]
            assert memory.session_records(sid)[0]["content"] == "network query"
            raise ModelServiceError("network unavailable", 503)

    provider = Offline()
    failed = runtime(config, memory, provider, tools=AsyncToolRegistry())
    assert not asyncio.run(failed.run("network query", sid)).completed
    restored = MemoryStore(config.memory_dir)
    clean = restored.restore_messages(sid)
    projected = restored.restore_messages(sid, provider_context=True)
    assert clean[0]["content"] == "network query"
    assert projected[0] == provider.calls[0][-1]
    online = Capture()
    resumed = runtime(config, restored, online, tools=AsyncToolRegistry())
    assert asyncio.run(resumed.run("retry please", sid)).completed
    assert online.calls[0][1] == provider.calls[0][-1]
    assert online.calls[0][-1]["content"] == "retry please"


def test_tool_iterations_keep_committed_user_projection(tmp_path):
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)

    class ToolProvider(Capture):
        async def complete(self, messages, tools):
            self.calls.append(copy.deepcopy(messages))
            if len(self.calls) == 1:
                memory.runtime_notice = "new notice visible next turn"
                return ModelReply(tool_calls=(ToolCall(id="calc", name="calculator",
                                                      arguments={"expression": "1+1"}),))
            return ModelReply(text="done")

    provider = ToolProvider()
    agent = runtime(config, memory, provider, tools=AsyncToolRegistry([CalculatorTool()]))
    result = asyncio.run(agent.run("calculate"))
    assert result.completed
    assert provider.calls[1][:len(provider.calls[0])] == provider.calls[0]
    assert "new notice" not in str(provider.calls[1])
    assert asyncio.run(agent.run("next", result.session_id)).completed
    assert "new notice" in provider.calls[2][-1]["content"]


def test_fragment_withdrawal_summary_dedup_and_provenance(tmp_path):
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    builder = AgentDynamicContextBuilder(config, memory)
    summary = '<continuity_fragment ephemeral="true">\nsummary\n</continuity_fragment>'
    recalled = '<relevant_memory ephemeral="true">\nremember\n</relevant_memory>'
    builder.fragments.set(sid, "continuity", summary)
    builder.fragments.set(sid, "memory", recalled)
    first = builder.prepare("q1", sid, origin_refs={"run_id": "run-1"})
    memory.record_user(sid, "q1", provider_context=first.model_dump(mode="json"))
    unchanged = builder.prepare("q2", sid, origin_refs={"run_id": "run-2"})
    assert unchanged.fragments == {}
    assert unchanged.origin_refs == {"run_id": "run-2"}
    assert unchanged.render("q2") == "q2"
    builder.fragments.remove(sid, "memory")
    withdrawn = builder.prepare("q2", sid)
    assert list(withdrawn.fragments) == ["memory"]
    assert '"active":false' in withdrawn.fragments["memory"]
    memory.record_user(sid, "q2", provider_context=withdrawn.model_dump(mode="json"))
    assert builder.prepare("q3", sid).fragments == {}
    builder.fragments.set(sid, "continuity", summary.replace("summary", "updated summary"))
    assert list(builder.prepare("q3", sid).fragments) == ["continuity"]


def test_withdrawal_does_not_reset_two_hour_clock_each_turn(tmp_path, monkeypatch):
    class Clock(datetime):
        hour = 1

        @classmethod
        def now(cls):
            return datetime(2026, 9, 10, cls.hour).astimezone()

    monkeypatch.setattr("prompt.runtime_context.datetime", Clock)
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    builder = AgentDynamicContextBuilder(config, memory)
    builder.fragments.set(sid, "memory", '<relevant_memory ephemeral="true">x</relevant_memory>')
    first = builder.prepare("q1", sid)
    memory.record_user(sid, "q1", provider_context=first.model_dump(mode="json"))
    builder.fragments.remove(sid, "memory")
    withdrawn = builder.prepare("q2", sid)
    memory.record_user(sid, "q2", provider_context=withdrawn.model_dump(mode="json"))
    Clock.hour = 2
    assert builder.prepare("q3", sid).fragments == {}
    Clock.hour = 4
    assert list(builder.prepare("q4", sid).fragments) == ["agent"]


def test_compaction_rebases_two_hour_clock(tmp_path, monkeypatch):
    class Clock(datetime):
        hour = 1

        @classmethod
        def now(cls):
            return datetime(2026, 9, 10, cls.hour).astimezone()

    monkeypatch.setattr("prompt.runtime_context.datetime", Clock)
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    builder = AgentDynamicContextBuilder(config, memory)
    first = builder.prepare("q1", sid)
    memory.record_user(sid, "q1", provider_context=first.model_dump(mode="json"))
    memory.record_assistant(sid, "a1")
    memory.rollover_with_summary(sid, "new compacted summary", memory.active_filename(sid))
    builder.fragments.set(
        sid, "continuity",
        '<continuity_fragment ephemeral="true">new compacted summary</continuity_fragment>',
    )
    Clock.hour = 2
    rebased = builder.prepare("q2", sid)
    assert '"current_time":"2026-09-10T02:00:00' in rebased.fragments["agent"]
    memory.record_user(sid, "q2", provider_context=rebased.model_dump(mode="json"))
    Clock.hour = 3
    assert builder.prepare("q3", sid).fragments == {}
    Clock.hour = 4
    assert list(builder.prepare("q4", sid).fragments) == ["agent"]


def test_context_corruption_and_query_binding_fail_closed(tmp_path):
    memory = MemoryStore(tmp_path / "memory")
    sid = memory.create_session("start")
    packet = ProviderContextRecord.create("original", "epoch", {"memory": "value"})
    raw = packet.model_dump(mode="json")
    raw["fragments"]["memory"] = "tampered"
    with pytest.raises(ValueError, match="hash mismatch"):
        memory.record_user(sid, "original", provider_context=raw)
    with pytest.raises(ValueError, match="different user query"):
        memory.record_user(sid, "other", provider_context=packet.model_dump(mode="json"))
    assert memory.session_records(sid) == []


def test_emergency_compression_amendment_replays_and_does_not_leak(tmp_path):
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    memory.record_user(sid, "old request")
    memory.record_assistant(sid, "old answer")

    class Compressor(Capture):
        async def complete(self, messages, tools):
            self.calls.append(copy.deepcopy(messages))
            return ModelReply(text=json.dumps({"context_summary_markdown": "compacted detail"}))

    class Overflow(Capture):
        async def complete(self, messages, tools):
            self.calls.append(copy.deepcopy(messages))
            if len(self.calls) == 1:
                raise ModelServiceError("maximum context length exceeded", 400)
            return ModelReply(text="done")

    provider, compressor = Overflow(), Compressor()
    agent = runtime(config, memory, provider, tools=AsyncToolRegistry(),
                    compression_provider_factory=lambda: compressor)
    assert asyncio.run(agent.run("current", sid)).completed
    assert len(provider.calls) == 2
    assert "compacted detail" in provider.calls[1][-1]["content"]
    all_records = memory.sessions.read_all_records_strict(sid)
    amendments = [r for _, r in all_records if r.role == "provider_context"]
    assert len(amendments) == 1
    assert len([r for _, r in all_records if r.role == "user" and r.content == "current"]) == 1
    recovered = MemoryStore(config.memory_dir)
    assert recovered.restore_messages(sid, provider_context=True)[-2] == provider.calls[1][-1]
    assert "agent_runtime_context" not in str(compressor.calls)
    assert "provider_context" not in str(compressor.calls)
    assert "compacted detail" not in str(recovered.restore_messages(sid))
    assert memory.sessions.append_once(sid, amendments[0].model_dump(mode="python", exclude_unset=True)) is False
    next_provider = Capture()
    next_agent = runtime(config, recovered, next_provider, tools=AsyncToolRegistry())
    assert asyncio.run(next_agent.run("next", sid)).completed
    assert next_provider.calls[0][-1]["content"] == "next"
    assert next_provider.calls[0][:-2] == provider.calls[1]
    # A later compaction drops the old baseline and supplies the new summary.
    result = asyncio.run(ContextProcessor(config, recovered, provider_factory=lambda: compressor).compress(sid))
    assert result.status == "compressed"
    assert asyncio.run(next_agent.run("after second compression", sid)).completed
    assert "compacted detail" in next_provider.calls[-1][-1]["content"]


def test_metadata_excluded_from_export_and_compression(tmp_path):
    from context_process.compression import _normalize_records
    packet = ProviderContextRecord.create("raw", "epoch", {
        "agent": '<agent_runtime_context ephemeral="true">\n{}\n</agent_runtime_context>',
    })
    record = {"role": "user", "content": "raw", "provider_context": packet.model_dump(mode="json")}
    assert _normalize_records([record]) == [{"role": "user", "content": "raw"}]
    assert SessionPersistenceProjection.from_runtime_messages([record]) == [{"role": "user", "content": "raw"}]
    assert SessionPersistenceProjection.from_runtime_messages([
        {"role": "user", "content": packet.render("raw")},
    ]) == [{"role": "user", "content": "raw"}]


def test_new_context_never_overwrites_prior_query(tmp_path):
    config = config_for(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    builder = AgentDynamicContextBuilder(config, memory)
    first = builder.prepare("same query", sid)
    memory.record_user(sid, "same query", provider_context=first.model_dump(mode="json"))
    memory.record_assistant(sid, "answer")
    builder.fragments.set(sid, "continuity", '<continuity_fragment ephemeral="true">new</continuity_fragment>')
    second = builder.prepare("same query", sid)
    memory.record_user(sid, "same query", provider_context=second.model_dump(mode="json"))
    projected = memory.restore_messages(sid, provider_context=True)
    assert projected[0]["content"] == first.render("same query")
    assert projected[2]["content"] == second.render("same query")
    assert context_baseline(memory.session_context_records(sid))["continuity"] == second.fragments["continuity"]
