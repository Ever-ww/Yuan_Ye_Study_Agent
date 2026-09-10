import asyncio
import copy
import json

import pytest

from Agent import AgentRuntime, load_runtime_config
from Agent.contracts import ModelReply, ToolCall
from context_process import ContextProcessor
from memory import MemoryStore, MemoryScope, MemoryWriteRequest
from memory.retrieval import access_snapshot
from tool import AsyncToolRegistry, ToolContext
from tools.session_history import SessionHistoryTool


class Capture:
    streaming = False

    def __init__(self, summary=None):
        self.requests = []
        self.summary = summary

    async def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        return ModelReply(text=json.dumps({"context_summary_markdown": self.summary})
                          if self.summary else "answer")


def test_summary_survives_turns_restart_and_disabled_recall(tmp_path):
    config = load_runtime_config(tmp_path, compression_threshold_tokens=0,
                                 memory_retrieval_enabled=False)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    memory.record_user(sid, "original")
    source = memory.active_filename(sid)
    memory.rollover_with_summary(sid, "mandatory continuity", source)
    provider = Capture()
    for question in ("first", "second"):
        runtime = AgentRuntime(config, provider=provider, memory=memory,
                               tools=AsyncToolRegistry(), enable_sandbox=False)
        assert asyncio.run(runtime.run(question, sid)).completed
        request = provider.requests[-1]
        contents = "\n".join(str(m.get("content", "")) for m in request)
        assert "mandatory continuity" in contents
        assert source in contents
        assert contents.count('<continuity_fragment ephemeral="true">') == 1
        if question == "second":
            assert request[-1]["content"] == "second"
            assert request[:len(provider.requests[0])] == provider.requests[0]
    assert all("continuity_fragment" not in (r.content or "")
               for _, r in memory.sessions.read_all_records_strict(sid))


def test_compression_includes_all_summaries_and_source_refs(tmp_path):
    config = load_runtime_config(tmp_path)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    first_segment = memory.active_filename(sid)
    for summary in ("oldest unique detail", "newer summary"):
        memory.record_user(sid, "history")
        memory.record_assistant(sid, "response")
        memory.rollover_with_summary(sid, summary, memory.active_filename(sid))
    memory.record_user(sid, "new history")
    memory.record_assistant(sid, "new response")
    provider = Capture("combined summary")
    result = asyncio.run(ContextProcessor(config, memory, provider_factory=lambda: provider).compress(sid))
    assert result.status == "compressed", result
    records = json.loads(provider.requests[0][-1]["content"])["session_records"]
    assert [r["content"] for r in records if r["role"] == "summary"] == [
        "oldest unique detail", "newer summary"]
    summary = memory.session_records(sid)[0]
    assert len(summary["summary_history_refs"]) == 2
    assert first_segment in summary["summary_original_segments"]
    assert first_segment in memory.latest_summary(sid)
    assert all(ref["segment"] and len(ref["sha256"]) == 64 for ref in summary["summary_source_refs"])


def test_summary_recall_opt_in(tmp_path):
    config = load_runtime_config(tmp_path)
    assert not config.memory_recall_summaries
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    memory.memory_writer.write(MemoryWriteRequest(
        scope=MemoryScope.SESSION, scope_key=sid, kind="summary", content="sqlite decision",
        source="compression", source_ref="test", pinned=True))
    for enabled in (False, True):
        access = access_snapshot(runtime_profile="interactive", workspace_root=str(tmp_path),
                                 session_id=sid, run_id=None, recall_summaries=enabled)
        result = memory.memory_retriever.retrieve("sqlite", access, session_id=sid)
        assert bool(result.selected) is enabled


def test_original_history_search_and_scope(tmp_path):
    memory = MemoryStore(tmp_path / ".yy" / "memory")
    sid = memory.create_session("start")
    memory.record_user(sid, "lost unique original detail")
    source = memory.active_filename(sid)
    memory.rollover_with_summary(sid, "short summary", source)
    other = memory.create_session("other")
    memory.record_user(other, "private unrelated")
    tool = SessionHistoryTool(memory)
    context = ToolContext(project_root=memory.workspace_root, session_id=sid)
    result = json.loads(asyncio.run(tool.run({"query": "unique", "segment": source}, context)))
    assert result["records"][0]["content"] == "lost unique original detail"
    assert len(result["records"][0]["sha256"]) == 64
    with pytest.raises(PermissionError):
        asyncio.run(tool.run({"segment": memory.active_filename(other)}, context))
    with pytest.raises(ValueError):
        AsyncToolRegistry([tool]).select([tool.name])


def test_summary_present_after_tool_and_not_duplicated(tmp_path):
    from tools.calculator import CalculatorTool

    class ToolProvider(Capture):
        async def complete(self, messages, tools):
            self.requests.append(copy.deepcopy(messages))
            if len(self.requests) == 1:
                return ModelReply(tool_calls=(ToolCall(
                    id="sum-call", name="calculator", arguments={"expression": "1+1"},
                ),))
            return ModelReply(text="done")

    config = load_runtime_config(tmp_path, compression_threshold_tokens=0)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("start")
    memory.record_user(sid, "old")
    memory.rollover_with_summary(sid, "mandatory latest", memory.active_filename(sid))
    provider = ToolProvider()
    runtime = AgentRuntime(config, provider=provider, memory=memory,
                           tools=AsyncToolRegistry([CalculatorTool()]), enable_sandbox=False)
    assert asyncio.run(runtime.run("calculate", sid)).completed
    assert len(provider.requests) == 2
    for request in provider.requests:
        content = "\n".join(str(m.get("content") or "") for m in request)
        assert content.count("mandatory latest") == 1


def test_raw_record_content_pagination_and_path_guard(tmp_path):
    memory = MemoryStore(tmp_path / ".yy" / "memory")
    sid = memory.create_session("start")
    memory.record_user(sid, "a" * 2000 + "tail detail")
    tool = SessionHistoryTool(memory)
    context = ToolContext(project_root=memory.workspace_root, session_id=sid)
    result = json.loads(asyncio.run(tool.run({"content_offset": 2000}, context)))
    assert result["records"][0]["content"] == "tail detail"
    with pytest.raises(PermissionError):
        asyncio.run(tool.run({"segment": "../secret.jsonl"}, context))


def test_harness_trace_catalog_includes_bound_history_tool(tmp_path):
    from run_ui.harness_loader import load_harness_module

    harness = load_harness_module()
    config = load_runtime_config(tmp_path)
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    profile, trace = harness._runtime_profile_and_trace(
        config, worktree, trigger="manual", target="extension", invocation_id="summary-test",
    )
    runtime = harness.create_coding_runtime(config, worktree, profile=profile, trace_context=trace)
    assert "session_history" in runtime.tools.names()
    assert "session_read" in runtime.tools.names()
    assert runtime.harness_trace_context.tool_catalog_hash == trace.tool_catalog_hash
    asyncio.run(runtime.close())


def test_history_tool_default_runtime_registration_is_isolated(tmp_path):
    config = load_runtime_config(tmp_path)
    for profile, expected in (("interactive", True), ("cron", False), ("maintenance", False)):
        runtime = AgentRuntime(
            config, runtime_profile=profile, enable_sandbox=False, enable_subagent=False,
        )
        assert ("session_history" in runtime.tools.names()) is expected
        assert ("session_read" in runtime.tools.names()) is expected
        asyncio.run(runtime.close())
