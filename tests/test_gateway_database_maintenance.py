from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from Agent.state import TaskState, TransitionCommand, WorkloadKind
from gateway.state_controller import StateController, StateInvariantError
from gateway.store import GatewayStore


def _controller(tmp_path: Path, **kwargs) -> tuple[GatewayStore, StateController, object]:
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    project = store.register_project(tmp_path)
    controller = StateController(
        store.database_path,
        gateway_epoch="maintenance-test",
        processed_command_compression_min_bytes=256,
        **kwargs,
    )
    state, duplicate = controller.create_run(
        run_id="run-maintenance",
        workload_kind=WorkloadKind.CHAT,
        project_id=project.project_id,
        client_id="client",
        task="database maintenance",
        idempotency_key="database-maintenance",
        request_hash=hashlib.sha256(b"database maintenance").hexdigest(),
    )
    assert duplicate is False
    return store, controller, state


def _transition(state) -> TransitionCommand:
    return TransitionCommand(
        command_id="command-maintenance",
        run_id=state.run_id,
        expected_revision=state.revision,
        gateway_epoch="maintenance-test",
        task_state=TaskState.QUEUED,
        reason="verify exact compressed command replay",
    )


def test_processed_command_result_is_compressed_and_replays_exactly(tmp_path: Path) -> None:
    store, controller, state = _controller(tmp_path)
    command = _transition(state)
    first = controller.apply(command)

    with store._connect() as connection:
        stored = str(connection.execute(
            "SELECT result_json FROM processed_commands WHERE command_id=?",
            (command.command_id,),
        ).fetchone()[0])
    wrapper = json.loads(stored)
    assert wrapper[StateController._PROCESSED_COMMAND_ENCODING_KEY] == "zlib-base64-v1"
    assert controller._decode_processed_command_result(stored) == first.model_dump_json()

    replay = controller.apply(command)
    assert replay.duplicate is True
    assert replay.model_copy(update={"duplicate": False}) == first


def test_legacy_command_compaction_is_bounded_and_hash_checked(tmp_path: Path) -> None:
    store, controller, state = _controller(tmp_path)
    command = _transition(state)
    first = controller.apply(command)
    raw = first.model_dump_json()
    with store._connect() as connection:
        connection.execute(
            "UPDATE processed_commands SET result_json=? WHERE command_id=?",
            (raw, command.command_id),
        )

    result = controller.compact_processed_commands(limit=1)
    assert result["compacted"] == 1
    assert result["bytes_saved"] > 0
    assert controller.apply(command).duplicate is True

    with store._connect() as connection:
        stored = str(connection.execute(
            "SELECT result_json FROM processed_commands WHERE command_id=?",
            (command.command_id,),
        ).fetchone()[0])
        wrapper = json.loads(stored)
        wrapper["content_sha256"] = "0" * 64
        connection.execute(
            "UPDATE processed_commands SET result_json=? WHERE command_id=?",
            (json.dumps(wrapper), command.command_id),
        )
    with pytest.raises(StateInvariantError, match="hash mismatch"):
        controller.apply(command)


def test_health_is_cached_and_reports_database_pressure(tmp_path: Path) -> None:
    _, controller, _ = _controller(
        tmp_path,
        health_cache_seconds=60,
        database_warning_bytes=1,
        database_critical_bytes=2,
    )
    calls = 0
    collect = controller._collect_health

    def counted():
        nonlocal calls
        calls += 1
        return collect()

    controller._collect_health = counted  # type: ignore[method-assign]
    first = controller.health()
    second = controller.health()

    assert calls == 1
    assert first["gateway_database_capacity_status"] == "critical"
    assert second["health_cache_age_seconds"] >= 0
    assert "processed_command_storage_bytes" in first


def test_full_integrity_check_is_explicit_not_a_health_side_effect(tmp_path: Path) -> None:
    store, _, _ = _controller(tmp_path)
    # A same-schema restart deliberately defers the expensive full scan.
    restarted = StateController(store.database_path, gateway_epoch="restart")
    assert restarted.health()["sqlite_quick_check"] == "pending"
    assert restarted.refresh_integrity_check() == "ok"
    assert restarted.health()["sqlite_quick_check"] == "ok"


def test_new_gateway_database_enables_incremental_auto_vacuum(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / ".yy" / "gateway")
    with sqlite3.connect(store.database_path) as connection:
        assert int(connection.execute("PRAGMA auto_vacuum").fetchone()[0]) == 2
