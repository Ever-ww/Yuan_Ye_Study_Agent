from __future__ import annotations

import json
import sqlite3
import asyncio
from contextlib import closing
from pathlib import Path

import pytest

from memory import (
    LegacyMemoryMigrator,
    MemoryCardinality,
    MemoryContextProjector,
    MemoryIndexWorker,
    MemoryRetrievalProfile,
    MemoryScope,
    MemoryStatus,
    MemoryWriteRequest,
    MemoryWriter,
    StructuredMemoryStore,
    MemoryEmbeddingWorker,
    MemoryProfileProjector,
    MemoryStore,
)
from Agent import load_runtime_config
from Agent.hook import HookEvent, HookPoint, HookRegistry
from memory.callbacks import register_memory_callbacks
from memory.retrieval import MemoryRetriever, access_snapshot, project_identity
from prompt import PromptComposer


def _request(content: str, **updates) -> MemoryWriteRequest:
    values = {
        "scope": MemoryScope.PROJECT,
        "scope_key": "project-a",
        "kind": "decision",
        "content": content,
        "source": "explicit_user",
        "source_ref": "session:test:1",
        "confidence": 0.95,
    }
    values.update(updates)
    return MemoryWriteRequest(**values)


def test_canonical_transaction_only_enqueues_index_and_projection(tmp_path: Path) -> None:
    store = StructuredMemoryStore(tmp_path / "memory")
    record = MemoryWriter(store).write(_request("Use Python 3.12"))

    assert not store.index_path.exists()
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT status FROM memory_index_jobs").fetchone()[0] == "pending"
        assert db.execute("SELECT status FROM memory_projection_jobs").fetchone()[0] == "pending"

    worker = MemoryIndexWorker(store)
    assert worker.reconcile() == 1
    # Simulate index UPSERT succeeding before the canonical READY commit.
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE memory_index_jobs SET status='pending' WHERE memory_id=?", (record.memory_id,))
        db.commit()
    assert worker.reconcile() == 1
    with closing(sqlite3.connect(worker.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_index WHERE memory_id=?", (record.memory_id,)).fetchone()[0] == 1


def test_supersede_requires_structural_single_subject_and_trusted_evidence(tmp_path: Path) -> None:
    store = StructuredMemoryStore(tmp_path / "memory")
    writer = MemoryWriter(store)
    old = writer.write(_request(
        "Python 3.11", subject_key="project.python_version",
        cardinality=MemoryCardinality.SINGLE,
    ))
    new = writer.write(_request(
        "Python 3.12", source_ref="session:test:2",
        subject_key="project.python_version", cardinality=MemoryCardinality.SINGLE,
        replace_existing=True,
    ))
    records = {item.memory_id: item for item in store.records_for_scopes(
        ((MemoryScope.PROJECT, "project-a"),),
        statuses=(MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED),
    )}
    assert records[old.memory_id].status is MemoryStatus.SUPERSEDED
    assert records[old.memory_id].superseded_by == new.memory_id

    cuda = writer.write(_request("User likes CUDA", kind="preference", source_ref="s:3"))
    cuda_13 = writer.write(_request("User likes CUDA 13", kind="preference", source_ref="s:4"))
    active = store.records_for_scopes(((MemoryScope.PROJECT, "project-a"),))
    assert {cuda.memory_id, cuda_13.memory_id}.issubset({item.memory_id for item in active})

    with pytest.raises(ValueError):
        _request("bad", replace_existing=True)

    writer.write(_request(
        "Python 3.13 maybe", source="model_inferred", source_ref="model:1",
        subject_key="project.python_version", cardinality=MemoryCardinality.SINGLE,
        replace_existing=True, confidence=0.4,
    ))
    conflicted = store.records_for_scopes(
        ((MemoryScope.PROJECT, "project-a"),), statuses=(MemoryStatus.CONFLICTED,),
    )
    assert len(conflicted) == 2


def test_scope_first_retrieval_and_prompt_projection(tmp_path: Path) -> None:
    store = StructuredMemoryStore(tmp_path / "memory")
    writer = MemoryWriter(store)
    wanted = writer.write(_request("Prefer concise technical explanations", pinned=True))
    writer.write(_request("Secret from another project", scope_key="project-b", source_ref="s:b"))
    MemoryIndexWorker(store).reconcile(limit=10)
    access = access_snapshot(
        runtime_profile="interactive", workspace_root="project-a", session_id="session-a", run_id="run-a",
    ).model_copy(update={
        "scopes": ((MemoryScope.PROJECT, "project-a"),),
        "profile": MemoryRetrievalProfile(max_records=1, token_budget=100),
    })
    result = MemoryRetriever(store).retrieve(
        "How should the explanation be written?", access, session_id="session-a",
    )
    assert [item.record.memory_id for item in result.selected] == [wanted.memory_id]
    rendered = MemoryContextProjector.render(result)
    assert "concise technical" in rendered
    assert "another project" not in rendered


def test_retrieval_evidence_failure_is_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StructuredMemoryStore(tmp_path / "memory")
    MemoryWriter(store).write(_request("Durable fact"))
    access = access_snapshot(
        runtime_profile="interactive", workspace_root="project-a", session_id="s", run_id=None,
    ).model_copy(update={"scopes": ((MemoryScope.PROJECT, "project-a"),)})
    retriever = MemoryRetriever(store)
    monkeypatch.setattr(store, "record_retrieval", lambda payload: (_ for _ in ()).throw(sqlite3.OperationalError("disk")))

    result = retriever.retrieve("Durable", access, session_id="s")

    assert result.selected
    assert retriever.retrieval_audit_failures == 1


def test_legacy_dream_and_markdown_deduplicate_to_evidence(tmp_path: Path) -> None:
    root = tmp_path / ".yy" / "memory"
    profile = root / "profile"
    profile.mkdir(parents=True)
    (profile / "USER.md").write_text("- Prefer concise answers <!-- dream:id=old -->\n", encoding="utf-8")
    dream = root.parent / "dream"
    dream.mkdir()
    (dream / "memories.json").write_text(json.dumps({
        "version": 1,
        "memories": {
            "mem_0123456789abcdef": {
                "status": "active", "statement": "Prefer concise answers",
                "target_file": "USER.md", "confidence": 0.9,
            },
        },
    }), encoding="utf-8")
    store = StructuredMemoryStore(root)

    result = LegacyMemoryMigrator(store, project_key="project-a").migrate()

    assert result == {"validated": 2, "imported": 1, "deduplicated": 1}
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0] == 2
    assert LegacyMemoryMigrator(store, project_key="project-a").migrate() == {
        "validated": 0, "imported": 0, "deduplicated": 0,
    }


def test_explicit_semantic_worker_is_versioned_and_retrieval_falls_back_safely(tmp_path: Path) -> None:
    class Provider:
        model = "memory-test"

        async def embed(self, texts):
            return tuple((1.0, 0.0) if "alpha" in text.casefold() else (0.0, 1.0) for text in texts)

    async def check() -> None:
        store = StructuredMemoryStore(tmp_path / "memory")
        writer = MemoryWriter(store)
        alpha = writer.write(_request("Alpha preference"))
        writer.write(_request("Beta preference", source_ref="s:beta"))
        MemoryIndexWorker(store).reconcile(limit=10)
        worker = MemoryEmbeddingWorker(store, Provider(), version=3)
        assert await worker.drain_once()
        assert await worker.drain_once()
        access = access_snapshot(
            runtime_profile="interactive", workspace_root="x", session_id="s", run_id=None,
        ).model_copy(update={"scopes": ((MemoryScope.PROJECT, "project-a"),)})
        retriever = MemoryRetriever(store)
        retriever.configure_semantic(Provider(), version=3)
        result = await retriever.retrieve_async("alpha", access, session_id="s")
        assert result.selected[0].record.memory_id == alpha.memory_id
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM memory_embedding_jobs WHERE embedding_version=3 AND status='ready'"
            ).fetchone()[0] == 2

    asyncio.run(check())


def test_projection_recovers_when_file_replace_preceded_ready_commit(tmp_path: Path) -> None:
    store = StructuredMemoryStore(tmp_path / "memory")
    writer = MemoryWriter(store)
    writer.write(_request("First durable fact"))
    projector = MemoryProfileProjector(store)
    assert projector.reconcile() == 1
    writer.write(_request("Second durable fact", source_ref="s:second"))

    records = store.records_for_scopes(((MemoryScope.PROJECT, "project-a"),))
    projection = projector._projections(MemoryScope.PROJECT, "project-a", records)[0]
    _, path, selected = projection
    # Simulate write+fsync+replace succeeding immediately before process death,
    # while the canonical projection job is still PENDING.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(projector._render(MemoryScope.PROJECT, selected))

    assert projector.reconcile() == 1
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM memory_projection_jobs WHERE status='ready'"
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM memory_projection_state WHERE status='drift'"
        ).fetchone()[0] == 0


def test_turn_start_freezes_recall_until_next_turn(tmp_path: Path) -> None:
    async def check() -> None:
        config = load_runtime_config(tmp_path)
        memory = MemoryStore(
            config.memory_dir, workspace_root=config.workspace_root, agent_root=config.agent_root,
        )
        session_id = memory.create_session("seed")
        project_key = project_identity(str(config.workspace_root))
        memory.memory_writer.write(_request(
            "Visible before turn", scope_key=project_key, source_ref="turn:before", pinned=True,
        ))
        memory.memory_index_worker.reconcile()
        prompts = PromptComposer(config, memory)
        hooks = HookRegistry()
        register_memory_callbacks(hooks, memory, prompts)

        await hooks.emit(HookEvent(
            point=HookPoint.TURN_START, session_id=session_id,
            data={"task": "durable fact", "config": config},
        ))
        memory.memory_writer.write(_request(
            "Committed after turn start", scope_key=project_key, source_ref="turn:after",
        ))
        memory.memory_index_worker.reconcile()
        messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "durable fact"}]
        await hooks.emit(HookEvent(
            point=HookPoint.MODEL_BEFORE, session_id=session_id,
            data={"task": "durable fact", "messages": messages, "first_model_call": True},
        ))
        messages[-1]["content"] = prompts.render_provider_query("durable fact", session_id)
        assert "Visible before turn" in messages[-1]["content"]
        assert "Committed after turn start" not in messages[-1]["content"]

        await hooks.emit(HookEvent(
            point=HookPoint.TURN_START, session_id=session_id,
            data={"task": "Committed after", "config": config},
        ))
        next_query = prompts.render_provider_query("Committed after", session_id)
        assert "Committed after turn start" in next_query

    asyncio.run(check())


def test_memory_eval_retrieves_three_relevant_records_from_one_hundred(tmp_path: Path) -> None:
    store = StructuredMemoryStore(tmp_path / "memory")
    writer = MemoryWriter(store)
    for number in range(97):
        writer.write(_request(
            f"Unrelated archival detail {number}", source_ref=f"eval:noise:{number}",
        ))
    expected = set()
    for number in range(3):
        expected.add(writer.write(_request(
            f"Quantum frobnitz relevant fact {number}", source_ref=f"eval:signal:{number}",
        )).memory_id)
    MemoryIndexWorker(store).reconcile(limit=200)
    access = access_snapshot(
        runtime_profile="interactive", workspace_root="unused", session_id="eval", run_id=None,
    ).model_copy(update={
        "scopes": ((MemoryScope.PROJECT, "project-a"),),
        "profile": MemoryRetrievalProfile(max_records=3, token_budget=200),
    })

    result = MemoryRetriever(store).retrieve("quantum frobnitz", access, session_id="eval")

    assert {item.record.memory_id for item in result.selected} == expected
