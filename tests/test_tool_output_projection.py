import asyncio
import copy
import hashlib
import json

import pytest

from Agent import load_runtime_config
from memory import MemoryStore
from memory.tool_projection import MARKER, ToolOutputProjectionPolicy, ToolOutputProjector
from tool import ToolContext
from tools.session_history import SessionHistoryTool


def group(ids, *, body=None, name="demo", status="success"):
    return [{"role": "assistant", "content": None, "tool_calls": [
        {"id": ident, "type": "function", "function": {"name": name, "arguments": "{}"}} for ident in ids
    ]}] + [{"role": "tool", "name": name, "tool_call_id": ident, "record_id": "record-" + ident,
            "status": status, "content": body or (ident * 12000)} for ident in ids]


def test_preview_25_characters_and_complete_parallel_group_protection():
    body = "甲" * 25 + "中" * 12000 + "乙" * 25
    original = group(["old"], body=body) + group(["a", "b", "c", "d"])
    messages = copy.deepcopy(original)
    projector = ToolOutputProjector(ToolOutputProjectionPolicy(), original)
    assert projector.project(messages) > 0
    assert messages[1]["content"].endswith("甲" * 25 + "\n…[中间已省略]…\n" + "乙" * 25)
    assert all(left == right for left, right in zip(messages[2:], original[2:]))
    assert "record-old" in messages[1]["content"]
    assert hashlib.sha256(body.encode()).hexdigest() in messages[1]["content"]
    frozen = copy.deepcopy(messages)
    assert projector.project(messages) == 0
    assert messages == frozen


def test_incomplete_orphan_and_current_groups_never_trimmed():
    old = group(["old"]) + group(["protected"])
    current = [{"role": "user", "content": "continue"}] + group(["current1"]) + group(["current2"])
    incomplete = group(["pending1", "pending2"])[:-1]
    orphan = [{"role": "tool", "name": "demo", "tool_call_id": "orphan", "content": "X" * 12000}]
    original = old + current + incomplete + orphan
    messages = copy.deepcopy(original)
    projector = ToolOutputProjector(ToolOutputProjectionPolicy(), old)
    projector.project(messages, protect_current_turn=True)
    assert messages[1]["content"].startswith(MARKER)
    assert messages[2:] == original[2:]


def test_recovered_current_run_is_not_mistaken_for_historical_output():
    old = group(["old"]) + group(["recent"])
    current = group(["current1"]) + group(["current2"])
    for record in current:
        record["run_id"] = "recovering-run"
    original = old + current
    messages = copy.deepcopy(original)
    ToolOutputProjector(ToolOutputProjectionPolicy(), original, current_run_id="recovering-run").project(messages)
    assert messages[1]["content"].startswith(MARKER)
    assert messages[2:] == original[2:]


def test_failure_excerpts_and_json_metadata_not_only_head_tail():
    log = "startup\n" + "noise\n" * 2000 + "FAILED tests/test_mid.py::test_case - AssertionError expected 3\n" + "noise\n" * 2000
    records = group(["log"], body=log, name="bash", status="error") + group(["last"])
    messages = copy.deepcopy(records)
    ToolOutputProjector(ToolOutputProjectionPolicy(), records).project(messages)
    assert "FAILED tests/test_mid.py" in messages[1]["content"]
    assert '"status":"error"' in messages[1]["content"]
    document = json.dumps({"path": "src/example.py", "format": "text", "offset_chars": 500,
                           "truncated": True, "content": "正文" * 9000}, ensure_ascii=False)
    records = group(["doc"], body=document, name="read_file") + group(["last"])
    messages = copy.deepcopy(records)
    ToolOutputProjector(ToolOutputProjectionPolicy(), records).project(messages)
    assert "path=src/example.py" in messages[1]["content"]
    assert "offset_chars=500" in messages[1]["content"]


def test_unproven_ambiguous_and_small_observations_are_preserved():
    records = group(["old"]) + group(["last"])
    for evidence in ([], [*records, records[1]]):
        messages = copy.deepcopy(records)
        assert ToolOutputProjector(ToolOutputProjectionPolicy(), evidence).project(messages) == 0
        assert messages == records
    small = group(["small"], body="only 60 chars" * 5) + group(["last"])
    assert ToolOutputProjector(ToolOutputProjectionPolicy(max_chars=50), small).project(copy.deepcopy(small)) == 0
    assert ToolOutputProjector(ToolOutputProjectionPolicy(max_chars=0), records).project(copy.deepcopy(records)) == 0


def test_frozen_turn_evidence_protection_and_reload_keep_canonical_intact(tmp_path):
    memory = MemoryStore(tmp_path / ".yy" / "memory")
    sid = memory.create_session("task")
    def write(ident):
        memory.record_user(sid, ident)
        memory.record_model_tool_calls(sid, content=None, tool_calls=group([ident])[0]["tool_calls"], model={}, model_call={})
        memory.record_tool_result(sid, tool_call_id=ident, name="demo", content=ident * 12000, status="success", arguments={})
        memory.record_assistant(sid, "done")
    write("old")
    write("recent")
    before = memory.sessions._active_path(sid).read_bytes()
    memory.prepare_historical_tool_outputs(sid, max_chars=10000)
    first = memory.restore_messages(sid)
    assert next(m for m in first if m.get("tool_call_id") == "old")["content"].startswith(MARKER)
    assert memory.sessions._active_path(sid).read_bytes() == before
    assert first == memory.refresh_messages(sid)
    write("current")
    later = memory.restore_messages(sid)
    for ident in ("recent", "current"):
        assert next(m for m in later if m.get("tool_call_id") == ident)["content"] == ident * 12000
    # Next Turn may now reduce 'recent', but still protects 'current'.
    memory.prepare_historical_tool_outputs(sid, max_chars=10000)
    assert next(m for m in memory.restore_messages(sid) if m.get("tool_call_id") == "recent")["content"].startswith(MARKER)
    assert all(MARKER not in str(record.get("content")) for record in memory.session_records(sid))


def test_exact_history_recall_across_segments_and_duplicate_call_ids(tmp_path):
    memory = MemoryStore(tmp_path / ".yy" / "memory")
    sid = memory.create_session("task")
    memory.record_user(sid, "first")
    raw = "begin" + "middle" * 2000 + "end"
    record_id = memory.record_tool_result(sid, tool_call_id="same", name="demo", content=raw,
                                         status="error", arguments={}, audit={"run_id": "r1"})
    memory.rollover_with_summary(sid, "summary", memory.active_filename(sid))
    memory.record_tool_result(sid, tool_call_id="same", name="demo", content="new result",
                              status="success", arguments={}, audit={"run_id": "r2"})
    tool = SessionHistoryTool(memory)
    context = ToolContext(project_root=memory.workspace_root, session_id=sid)
    def read(args):
        return json.loads(asyncio.run(tool.run(args, context)))
    assert read({"tool_call_id": "same"})["ambiguous"]
    selected = read({"record_id": record_id, "expected_content_hash": hashlib.sha256(raw.encode()).hexdigest()})["records"][0]
    assert selected["run_id"] == "r1" and selected["name"] == "demo" and selected["status"] == "error"
    assert selected["next_content_offset"] == 2000
    reconstructed = ""
    offset = 0
    while offset is not None:
        record = read({"record_id": record_id, "content_offset": offset})["records"][0]
        reconstructed += record["content"]
        offset = record["next_content_offset"]
    assert reconstructed == raw
    assert read({"tool_call_id": "same", "run_id": "r2"})["records"][0]["content"] == "new result"
    with pytest.raises(ValueError, match="hash mismatch"):
        read({"record_id": record_id, "expected_content_hash": "0" * 64})
    other = memory.create_session("other")
    other_context = ToolContext(project_root=memory.workspace_root, session_id=other)
    assert not json.loads(asyncio.run(tool.run({"record_id": record_id}, other_context)))["records"]


def test_preview_policy_config_defaults_and_validation(tmp_path):
    config = load_runtime_config(tmp_path)
    policy = ToolOutputProjectionPolicy.from_config(config)
    assert (policy.head_chars, policy.tail_chars, policy.protect_recent_groups) == (25, 25, 1)
    with pytest.raises(ValueError):
        load_runtime_config(tmp_path, tool_output_protect_recent_groups=0)


def test_gateway_tool_result_query_is_scoped_read_only_and_authenticated(tmp_path):
    from fastapi.testclient import TestClient
    from gateway.application import GatewayApplication
    from gateway.api import create_gateway_api

    config = load_runtime_config(tmp_path, dream_enabled=False, backup_enabled=False)
    def forbidden_runtime(*args, **kwargs):
        raise AssertionError("history query must not create a Runtime")
    application = GatewayApplication(config, runtime_factory=forbidden_runtime)
    project = application.store.register_project(tmp_path)
    memory = MemoryStore(config.memory_dir, workspace_root=tmp_path, agent_root=config.agent_root)
    sid = memory.create_session("history")
    record_id = memory.record_tool_result(sid, tool_call_id="lookup", name="demo", content="saved output",
                                         status="success", arguments={})
    path = f"/api/v1/projects/{project.project_id}/sessions/{sid}/tool-results"
    with TestClient(create_gateway_api(application, access_token="test-token")) as client:
        assert client.get(path, params={"record_id": record_id}).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        response = client.get(path, params={"record_id": record_id}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["records"][0]["content"] == "saved output"
        assert client.get(path, params={"record_id": record_id, "content_offset": -1}, headers=headers).status_code == 400
        assert client.get(path, headers=headers).status_code == 400
        (tmp_path / "other-project").mkdir()
        another = application.store.register_project(tmp_path / "other-project")
        wrong = f"/api/v1/projects/{another.project_id}/sessions/{sid}/tool-results"
        assert client.get(wrong, params={"record_id": record_id}, headers=headers).status_code == 404
        with application.store._connect() as db:
            assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_cli_tool_result_is_paginated_without_model_or_tool_execution(monkeypatch):
    import io
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from rich.console import Console
    from run_ui import cli

    reader = AsyncMock(return_value={"records": [{
        "content": "[bold]literal result[/bold]", "name": "read_file", "status": "success",
        "record_id": "exact-id", "content_sha256": "a" * 64, "next_content_offset": 2000,
    }]})
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=120))
    asyncio.run(cli._handle_tool_result_command(SimpleNamespace(session_tool_result=reader), "p", "s", "/tool-result exact-id"))
    assert "[bold]literal result[/bold]" in output.getvalue()
    assert "/tool-result exact-id 2000" in output.getvalue()
    reader.assert_awaited_once_with("p", "s", record_id="exact-id", content_offset=0)
    from gateway.models import GatewayEventEnvelope
    async def subscribe(*args):
        for sequence, event_type, payload in (
            (1, "tool_completed", {"name": "demo", "status": "success", "observation_id": "exact-id"}),
            (2, "run_completed", {"answer": "done"}),
        ):
            yield GatewayEventEnvelope(event_id=f"event-{sequence}", run_id="run", project_id="p", session_id="s",
                                       sequence=sequence, timestamp="2026-09-08T00:00:00+08:00",
                                       type=event_type, payload=payload)
    client = SimpleNamespace(start_run=AsyncMock(return_value=SimpleNamespace(run_id="run")),
                             subscribe=subscribe, acknowledge_run_result=AsyncMock())
    asyncio.run(cli._render_gateway(client, "p", "task", "s"))
    assert "详情：/tool-result exact-id" in output.getvalue()


@pytest.mark.parametrize("history_available", [True, False])
def test_runtime_hook_preview_requires_history_tool_and_keeps_prefix(tmp_path, history_available):
    from Agent import AgentRuntime
    from Agent.contracts import ModelReply
    from tool import AsyncToolRegistry

    config = load_runtime_config(tmp_path, compression_threshold_tokens=0)
    memory = MemoryStore(config.memory_dir, workspace_root=config.workspace_root, agent_root=config.agent_root)
    sid = memory.create_session("task")
    for ident in ("old", "recent"):
        memory.record_user(sid, ident)
        memory.record_model_tool_calls(sid, content=None, tool_calls=group([ident])[0]["tool_calls"], model={}, model_call={})
        memory.record_tool_result(sid, tool_call_id=ident, name="demo", content=ident * 12000, status="success", arguments={})
        memory.record_assistant(sid, "done")
    class Provider:
        streaming = False
        def __init__(self): self.requests = []
        async def complete(self, messages, tools):
            self.requests.append(copy.deepcopy(messages))
            return ModelReply(text="answer")
    provider = Provider()
    registry = AsyncToolRegistry([SessionHistoryTool(memory)] if history_available else [])
    runtime = AgentRuntime(config, memory=memory, tools=registry, provider=provider, enable_sandbox=False)
    async def check():
        for question in ("first", "second"):
            assert (await runtime.run(question, sid)).completed
        await runtime.close()
    asyncio.run(check())
    for request in provider.requests:
        old = next(m for m in request if m.get("tool_call_id") == "old")["content"]
        assert old.startswith(MARKER) is history_available
        assert next(m for m in request if m.get("tool_call_id") == "recent")["content"] == "recent" * 12000
    assert provider.requests[0][0] == provider.requests[1][0]
    assert all(MARKER not in str(record.get("content")) for record in memory.session_records(sid))


def test_budget_recheck_uses_same_projector_and_does_not_retrim(tmp_path):
    from context_process import ContextProcessor
    config = load_runtime_config(tmp_path, compression_threshold_tokens=1000)
    memory = MemoryStore(config.memory_dir)
    sid = memory.create_session("task")
    records = group(["old"]) + group(["recent"])
    memory._tool_projectors[sid] = ToolOutputProjector(ToolOutputProjectionPolicy(), records)
    messages = [{"role": "system", "content": "stable"}, *copy.deepcopy(records),
                {"role": "user", "content": "now"}, *group(["current"])]
    processor = ContextProcessor(config, memory)
    estimate = processor.finalize_request(sid, messages, [])
    assert estimate.projected_tool_output_tokens > 0
    first = copy.deepcopy(messages)
    processor.finalize_request(sid, messages, [])
    assert messages == first
    assert next(m for m in messages if m.get("tool_call_id") == "recent")["content"] == "recent" * 12000
