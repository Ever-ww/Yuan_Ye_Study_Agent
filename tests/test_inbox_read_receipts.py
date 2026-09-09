"""Read receipts follow displayed results, not Session access or transport choice."""
import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from gateway.client import GatewayClient
from gateway.models import GatewayEventEnvelope
from run_ui import cli


def inbox_item(run="run", **updates):
    return dict(item_id=f"item-{run}", run_id=run, project_id="project",
                session_id="session", read=False, **updates)


@pytest.mark.parametrize("http_fallback", [False, True])
@pytest.mark.parametrize("event_type", ["run_completed", "run_failed", "run_cancelled", "run_interrupted"])
def test_displayed_terminal_acknowledged_on_both_transports(monkeypatch, http_fallback, event_type):
    client = object.__new__(GatewayClient)
    client.start_run = AsyncMock(return_value=SimpleNamespace(run_id="run"))
    client.run = AsyncMock(return_value=SimpleNamespace(status="completed"))
    event = GatewayEventEnvelope(
        event_id="event", run_id="run", project_id="project", session_id="session",
        sequence=1, timestamp="2026-09-07T00:00:00+08:00", type=event_type,
        payload={"answer": "shown result", "message": "shown result"},
    )
    displayed = []
    marked = []

    class Live:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def update(self, panel, *, refresh=False):
            if refresh:
                displayed.append(str(panel.renderable))

    async def events(*args, **kwargs):
        if http_fallback:
            raise ConnectionError("WebSocket unavailable")
        yield event

    async def request(method, path, **kwargs):
        if path.endswith("/events"):
            return [event.model_dump(mode="json")]
        if path == "/api/v1/inbox":
            return [inbox_item(), inbox_item("background")]
        assert method == "POST" and path == "/api/v1/inbox/item-run/read"
        assert displayed and "shown result" in displayed[-1]
        marked.append(path)
        return {"read": True}

    client.events = events
    client._request = request
    monkeypatch.setattr(cli, "Live", Live)
    result = asyncio.run(cli._render_gateway(client, "project", "query", "session"))
    assert result[0] == "session"
    assert len(marked) == 1
    assert client.start_run.await_count == 1


def test_history_receipts_require_displayed_terminal_identity_and_exact_scope():
    client = object.__new__(GatewayClient)
    records = [
        dict(role="assistant", content="final", record_id="r1", run_id="shown"),
        dict(role="assistant", content="cancelled", record_id="r2", run_id="cancelled", status="cancelled"),
        dict(role="assistant", content="thinking", record_id="r3", run_id="partial", tool_calls=[{}]),
        dict(role="user", content="question", record_id="r4", run_id="user-only"),
        dict(role="summary", content="rolling", record_id="r5", run_id="summary"),
        dict(role="assistant", content="legacy", run_id="no-record-id"),
        dict(role="assistant", content="background", record_id="r6", run_id="cron", origin="cron"),
    ]
    items = [inbox_item(run) for run in
             ("shown", "cancelled", "partial", "user-only", "summary", "no-record-id", "cron", "unseen")]
    items += [dict(inbox_item("shown"), project_id="other", item_id="other-project"),
              dict(inbox_item("shown"), session_id="other", item_id="other-session")]
    marked = []

    async def request(method, path, **kwargs):
        if method == "GET":
            return [item for item in items if item["item_id"] not in marked]
        item_id = path.split("/")[-2]
        marked.append(item_id)
        return {"read": True}

    client._request = request

    async def check():
        await client.acknowledge_session_history("project", "session", records)
        await client.acknowledge_session_history("project", "session", records)

    asyncio.run(check())
    assert marked == ["item-shown", "item-cancelled"]


def test_receipt_failure_does_not_fail_displayed_chat(monkeypatch):
    client = object.__new__(GatewayClient)
    client.start_run = AsyncMock(return_value=SimpleNamespace(run_id="run"))
    client.acknowledge_run_result = AsyncMock(side_effect=ConnectionError("offline"))
    async def subscribe(*args):
        yield GatewayEventEnvelope(
            event_id="event", run_id="run", project_id="project", session_id="session",
            sequence=1, timestamp="2026-09-07T00:00:00+08:00", type="run_completed",
            payload={"answer": "finished"},
        )
    client.subscribe = subscribe
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=120))
    result = asyncio.run(cli._render_gateway(client, "project", "query", "session"))
    assert result == ("session", "")
    assert "已读确认失败" in output.getvalue()
    assert client.start_run.await_count == 1


def test_restore_acknowledges_only_after_rendering(monkeypatch):
    client = SimpleNamespace(
        sessions=AsyncMock(return_value=[{"session_id": "session"}]),
        session=AsyncMock(return_value=[{"role": "assistant", "content": "restored"}]),
        inbox=AsyncMock(return_value=[]),
    )
    output = io.StringIO()
    async def acknowledge(project_id, session_id, records):
        assert "restored" in output.getvalue()
        assert (project_id, session_id) == ("project", "session")
    client.acknowledge_session_history = AsyncMock(side_effect=acknowledge)
    console = Console(file=output, width=120)
    monkeypatch.setattr(console, "input", lambda *args: "/exit")
    monkeypatch.setattr(cli, "console", console)
    monkeypatch.setattr(cli, "_gateway_client", lambda: client)
    monkeypatch.setattr(cli, "_gateway_project", AsyncMock(return_value={"project_id": "project"}))
    asyncio.run(cli._chat_gateway("session", interrupt_controller=cli.ChatInterruptController()))
    assert client.acknowledge_session_history.await_count == 1
