from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from Agent.state import RecordRuntimeEventCommand, WorkloadKind
from gateway.event_store import (
    EventConsumerCursorStore,
    EventStore,
    EventStoreIntegrityError,
    GatewayEventArchiveService,
    ProjectionRebuilder,
    canonical_json,
)
from gateway.events import GatewayEventBus
from gateway.models import GatewayEventEnvelope
from gateway.outbox import OutboxDispatcher
from gateway.state_controller import StateConflictError, StateController
from gateway.store import GatewayStore


@pytest.fixture()
def event_system(tmp_path: Path):
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    controller = StateController(store.database_path, gateway_epoch="epoch")
    state, _ = controller.create_run(
        run_id="run-event-store", workload_kind=WorkloadKind.CHAT,
        project_id="project", client_id="client", task="task",
        idempotency_key="idempotency", request_hash="0" * 64,
    )
    return store, controller, state


def test_command_event_key_is_unique_but_event_type_is_not(event_system) -> None:
    _store, controller, state = event_system
    with controller._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        first = controller._write_event(
            connection, state, "same_type", {"value": 1},
            command_id="one-command", event_key="first",
        )
        second = controller._write_event(
            connection, state, "same_type", {"value": 2},
            command_id="one-command", event_key="second",
        )
        connection.commit()
    assert first.event_id != second.event_id
    with controller._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            controller._write_event(
                connection, state, "another_type", {"value": 3},
                command_id="one-command", event_key="first",
            )
        connection.rollback()


def test_delivery_policy_is_frozen_and_outbox_completion_uses_snapshot(event_system) -> None:
    _store, controller, state = event_system
    with controller._connection() as connection:
        old = connection.execute(
            "SELECT required,sink_config_version,sink_config_hash FROM event_deliveries "
            "WHERE event_id=? AND sink_id='jsonl'",
            (controller.events(state.run_id)[0].event_id,),
        ).fetchone()
        connection.execute(
            "UPDATE event_sinks SET required_by_default=0,current_config_version=2,"
            "current_config_hash=? WHERE sink_id='jsonl'",
            ("f" * 64,),
        )
        frozen = connection.execute(
            "SELECT required,sink_config_version,sink_config_hash FROM event_deliveries "
            "WHERE event_id=? AND sink_id='jsonl'",
            (controller.events(state.run_id)[0].event_id,),
        ).fetchone()
    assert tuple(old) == tuple(frozen)
    assert frozen["required"] == 1


def test_delivery_claim_is_revision_cas_and_has_one_active_attempt(event_system) -> None:
    store, _controller, _state = event_system
    dispatcher = OutboxDispatcher(
        store.database_path, store.runs_directory,
        GatewayEventBus().deliver_from_outbox, gateway_epoch="epoch",
    )
    due = dispatcher._due_deliveries()
    claim = dispatcher._claim(due[0]["delivery_id"], due[0]["revision"])
    assert claim is not None
    assert dispatcher._claim(due[0]["delivery_id"], due[0]["revision"]) is None
    with dispatcher._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO event_delivery_attempts VALUES(?,?,?,?, 'delivering',?,NULL,NULL,NULL)",
                (uuid4().hex, claim.delivery_id, claim.attempt_no + 1, "other", "now"),
            )


def test_jsonl_receives_exact_canonical_event_bytes(event_system) -> None:
    store, controller, state = event_system
    dispatcher = OutboxDispatcher(
        store.database_path, store.runs_directory,
        GatewayEventBus().deliver_from_outbox, gateway_epoch="epoch",
    )
    asyncio.run(dispatcher.drain_once())
    raw = store.runs_directory.joinpath(f"{state.run_id}.jsonl").read_text(encoding="utf-8").splitlines()[0]
    with controller._connection() as connection:
        canonical = connection.execute(
            "SELECT canonical_event_json FROM gateway_events WHERE stream_id=? ORDER BY stream_sequence LIMIT 1",
            (state.run_id,),
        ).fetchone()[0]
    assert raw == canonical


def test_archive_freezes_members_and_uses_utf8_byte_offsets(event_system, tmp_path: Path) -> None:
    _store, controller, state = event_system
    state = controller.apply(RecordRuntimeEventCommand(
        command_id="utf8-event", run_id=state.run_id, expected_revision=state.revision,
        gateway_epoch="epoch", event_type="unicode", payload={"text": "论文与证据"},
    )).state
    with controller._connection() as connection:
        event_ids = [row[0] for row in connection.execute(
            "SELECT event_id FROM gateway_events WHERE stream_id=? ORDER BY stream_sequence",
            (state.run_id,),
        ).fetchall()]
    archive = GatewayEventArchiveService(controller.database_path, tmp_path / "archives")
    archive_id = archive.prepare(state.run_id, event_ids)
    # A later event cannot silently enter the already durable PREPARING member set.
    controller.apply(RecordRuntimeEventCommand(
        command_id="later-event", run_id=state.run_id, expected_revision=state.revision,
        gateway_epoch="epoch", event_type="later", payload={"value": 3},
    ))
    archive.write_and_verify(archive_id)
    with controller._connection() as connection:
        archive_row = connection.execute(
            "SELECT * FROM gateway_event_archives WHERE archive_id=?", (archive_id,),
        ).fetchone()
        members = connection.execute(
            "SELECT * FROM gateway_event_archive_members WHERE archive_id=? ORDER BY stream_sequence",
            (archive_id,),
        ).fetchall()
    assert len(members) == len(event_ids)
    assert archive_row["events_content_hash"] != archive_row["manifest_hash"]
    data = Path(archive_row["archive_path"]).read_bytes()
    for member in members:
        raw = data[member["byte_offset"]:member["byte_offset"] + member["byte_length"]]
        assert hashlib.sha256(raw).hexdigest() == member["canonical_hash"]
        json.loads(raw.decode("utf-8"))
    restored = EventStore(controller.database_path).read_stream(state.run_id)
    assert [item.event_id for item in restored[:len(event_ids)]] == event_ids


def test_fk_retention_and_cursor_invariants(event_system) -> None:
    _store, controller, state = event_system
    with controller._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO event_outbox VALUES('orphan','missing','now',NULL,0)",
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO event_delivery_attempts VALUES('orphan','missing',1,'e',"
                "'delivering','now',NULL,NULL,NULL)",
            )
    before = len(controller.events(state.run_id))
    assert controller.prune_retention()["gateway_events"] == 0
    assert len(controller.events(state.run_id)) == before
    with controller._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision = EventConsumerCursorStore.advance_in_transaction(
            connection, consumer_id="projection", stream_id=state.run_id,
            expected_revision=0, processed_sequence=1,
        )
        connection.commit()
    assert revision == 1
    with controller._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(EventStoreIntegrityError):
            EventConsumerCursorStore.advance_in_transaction(
                connection, consumer_id="projection", stream_id=state.run_id,
                expected_revision=0, processed_sequence=2,
            )
        connection.rollback()


def test_projection_rebuild_and_legacy_scan_scope(tmp_path: Path) -> None:
    gateway_root = tmp_path / ".yy" / "gateway"
    store = GatewayStore(gateway_root)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO runs(run_id,project_id,session_id,client_id,task,status,created_at) "
            "VALUES('legacy-run','project',NULL,'client','task','completed','2020-01-01')",
        )
        connection.execute("INSERT INTO event_sequences VALUES('legacy-run',0)")
    event = GatewayEventEnvelope(
        event_id="legacy-event", sequence=1, timestamp="2020-01-01",
        project_id="project", run_id="legacy-run", type="legacy",
    )
    (store.runs_directory / "legacy-run.jsonl").write_text(
        event.model_dump_json() + "\n", encoding="utf-8",
    )
    # A damaged non-Gateway JSONL outside runs_directory must never be scanned.
    unrelated = tmp_path / ".yy" / "memory" / "session.jsonl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("not a gateway event\n", encoding="utf-8")
    controller = StateController(store.database_path, gateway_epoch="migration")
    assert controller.events("legacy-run")[0].event_id == "legacy-event"
    with controller._connection() as connection:
        statuses = [row[0] for row in connection.execute(
            "SELECT status FROM event_deliveries WHERE event_id='legacy-event'",
        ).fetchall()]
    assert statuses == ["delivered", "delivered"]

    (store.runs_directory / "legacy-run.jsonl").unlink()
    rebuilt = ProjectionRebuilder(
        EventStore(store.database_path), store.runs_directory,
    ).rebuild_jsonl("legacy-run")
    assert json.loads(rebuilt.read_text(encoding="utf-8"))["event_id"] == "legacy-event"
    # Reinitialization is idempotent and does not rebroadcast historical deliveries.
    StateController(store.database_path, gateway_epoch="migration-2")
    with controller._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_events WHERE event_id='legacy-event'",
        ).fetchone()[0] == 1


def test_event_authority_architecture_has_no_legacy_store_writers() -> None:
    forbidden = {
        "append_event", "read_events", "event_path", "create_run", "update_run",
        "create_inbox", "save_approval", "decide_approval",
    }
    assert forbidden.isdisjoint(GatewayStore.__dict__)
    root = Path(__file__).resolve().parents[1]
    application_source = (root / "gateway" / "application.py").read_text(encoding="utf-8")
    assert "store.read_events" not in application_source
    assert "events.publish" not in application_source


def test_v9_delivery_columns_migrate_once_without_rewriting_event(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    event = GatewayEventEnvelope(
        event_id="v9-event", sequence=1, timestamp="2020-01-01",
        project_id="project", run_id="v9-run", type="v9",
    )
    raw = event.model_dump_json()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO runs(run_id,project_id,session_id,client_id,task,status,created_at) "
            "VALUES('v9-run','project',NULL,'client','task','completed','2020-01-01')",
        )
        connection.execute("INSERT INTO event_sequences VALUES('v9-run',1)")
        connection.execute(
            "CREATE TABLE gateway_events(event_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,"
            "sequence INTEGER NOT NULL,event_json TEXT NOT NULL,created_at TEXT NOT NULL,"
            "UNIQUE(run_id,sequence))",
        )
        connection.execute(
            "CREATE TABLE event_outbox(event_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,"
            "sequence INTEGER NOT NULL,eventbus_status TEXT NOT NULL,jsonl_status TEXT NOT NULL,"
            "eventbus_attempts INTEGER NOT NULL,jsonl_attempts INTEGER NOT NULL,last_error TEXT,"
            "delivered_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
        )
        connection.execute(
            "INSERT INTO gateway_events VALUES('v9-event','v9-run',1,?,'2020-01-01')", (raw,),
        )
        connection.execute(
            "INSERT INTO event_outbox VALUES('v9-event','v9-run',1,'sent','sent',1,1,NULL,"
            "'2020-01-01','2020-01-01','2020-01-01')",
        )
        connection.execute("PRAGMA user_version=9")
    controller = StateController(store.database_path, gateway_epoch="v10")
    with controller._connection() as connection:
        row = connection.execute(
            "SELECT canonical_event_json,canonical_hash FROM gateway_events WHERE event_id='v9-event'",
        ).fetchone()
        deliveries = connection.execute(
            "SELECT status FROM event_deliveries WHERE event_id='v9-event'",
        ).fetchall()
    assert row["canonical_event_json"] == raw
    assert row["canonical_hash"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert [item["status"] for item in deliveries] == ["delivered", "delivered"]
