"""SQLite canonical store and eventually-consistent memory projections."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, Sequence
from uuid import uuid4
import re

from .long_term import (
    MemoryCardinality,
    MemoryEvidence,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryWriteRequest,
    content_digest,
    normalize_memory_content,
    utc_now,
)


_SCHEMA_VERSION = 1


class StructuredMemoryStore:
    """Canonical long-term memory facts and durable projection intents."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / "memory.sqlite3"
        self.index_path = self.root / "index.sqlite3"
        self.profile_root = self.root / "profile"
        self.root.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(path or self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if path is None:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_schema (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO memory_schema(singleton, version) VALUES(1, 1);
                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject_key TEXT,
                    cardinality TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    primary_source_ref TEXT NOT NULL,
                    importance REAL NOT NULL,
                    confidence REAL NOT NULL,
                    pinned INTEGER NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT,
                    status TEXT NOT NULL,
                    supersedes TEXT,
                    superseded_by TEXT,
                    content_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, scope_key, kind, content_hash),
                    FOREIGN KEY(supersedes) REFERENCES memory_records(memory_id) ON DELETE RESTRICT,
                    FOREIGN KEY(superseded_by) REFERENCES memory_records(memory_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS memory_scope_status
                    ON memory_records(scope, scope_key, status, kind);
                CREATE INDEX IF NOT EXISTS memory_subject
                    ON memory_records(scope, scope_key, kind, subject_key, status);
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_hash TEXT,
                    locator TEXT,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memory_records(memory_id) ON DELETE RESTRICT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS memory_evidence_identity
                    ON memory_evidence(memory_id, source, source_ref, COALESCE(locator, ''));
                CREATE TABLE IF NOT EXISTS memory_mutations (
                    mutation_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    mutation_type TEXT NOT NULL,
                    before_revision INTEGER,
                    after_revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memory_records(memory_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS memory_index_jobs (
                    job_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    target_revision INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(memory_id, target_revision, operation),
                    FOREIGN KEY(memory_id) REFERENCES memory_records(memory_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS memory_projection_jobs (
                    job_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    target_watermark INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, scope_key, target_watermark)
                );
                CREATE TABLE IF NOT EXISTS memory_embedding_jobs (
                    job_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    target_revision INTEGER NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(memory_id, target_revision, embedding_model, embedding_version),
                    FOREIGN KEY(memory_id) REFERENCES memory_records(memory_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS memory_retrievals (
                    retrieval_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    turn_id TEXT,
                    query_hash TEXT NOT NULL,
                    store_watermark INTEGER NOT NULL,
                    allowed_scope_hashes_json TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    selected_memory_ids_json TEXT NOT NULL,
                    score_components_json TEXT NOT NULL,
                    token_budget INTEGER NOT NULL,
                    used_tokens INTEGER NOT NULL,
                    index_version TEXT,
                    duration_ms REAL NOT NULL,
                    degradation_reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_retrieval_feedback (
                    retrieval_id TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    selected_memory_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(retrieval_id) REFERENCES memory_retrievals(retrieval_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS memory_migrations (
                    migration_id TEXT PRIMARY KEY,
                    migration_version INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    validated_count INTEGER NOT NULL,
                    imported_count INTEGER NOT NULL,
                    deduplicated_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(migration_version, source_path, source_hash)
                );
                CREATE TABLE IF NOT EXISTS memory_projection_state (
                    projection_id TEXT PRIMARY KEY,
                    watermark INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    def quick_check(self) -> bool:
        with closing(self._connect()) as db:
            return db.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def watermark(self) -> int:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM memory_mutations").fetchone()
            return int(row[0])

    def write(self, request: MemoryWriteRequest, *, supersede_threshold: float = 0.8) -> MemoryRecord:
        normalized = normalize_memory_content(request.content)
        digest = content_digest(request.content)
        now = utc_now()
        with self.transaction() as db:
            duplicate = db.execute(
                "SELECT * FROM memory_records WHERE scope=? AND scope_key=? AND kind=? AND content_hash=?",
                (request.scope.value, request.scope_key, request.kind, digest),
            ).fetchone()
            if duplicate is not None:
                record = self._row_record(duplicate)
                self._append_evidence(db, record.memory_id, request, now)
                self._mutation(db, record.memory_id, "evidence_appended", record.revision, record.revision, {
                    "source": request.source, "source_ref": request.source_ref,
                }, now)
                return record

            if request.pinned:
                pinned_count = int(db.execute(
                    """SELECT COUNT(*) FROM memory_records
                       WHERE scope=? AND scope_key=? AND pinned=1 AND status='active'""",
                    (request.scope.value, request.scope_key),
                ).fetchone()[0])
                if pinned_count >= 10:
                    raise ValueError("a memory scope may contain at most 10 active pinned records")

            prior: sqlite3.Row | None = None
            can_replace = (
                request.replace_existing
                and request.cardinality is MemoryCardinality.SINGLE
                and bool(request.subject_key)
                and request.source in {"explicit_user", "system_observed"}
                and request.confidence >= supersede_threshold
            )
            if request.replace_existing and request.cardinality is MemoryCardinality.SINGLE and request.subject_key:
                prior = db.execute(
                    """SELECT * FROM memory_records
                       WHERE scope=? AND scope_key=? AND kind=? AND subject_key=?
                         AND cardinality='single' AND status='active'
                       ORDER BY revision DESC, created_at DESC LIMIT 1""",
                    (request.scope.value, request.scope_key, request.kind, request.subject_key),
                ).fetchone()
            memory_id = "mem_" + hashlib.sha256(
                f"{request.scope.value}\0{request.scope_key}\0{request.kind}\0{digest}".encode("utf-8")
            ).hexdigest()[:24]
            db.execute(
                """INSERT INTO memory_records(
                    memory_id, scope, scope_key, kind, subject_key, cardinality, content,
                    normalized_content, source, primary_source_ref, importance, confidence,
                    pinned, valid_from, valid_until, status, supersedes, superseded_by,
                    content_hash, revision, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, request.scope.value, request.scope_key, request.kind,
                 request.subject_key, request.cardinality.value, request.content, normalized,
                 request.source, request.source_ref, request.importance, request.confidence,
                 int(request.pinned), request.valid_from, request.valid_until,
                 (
                     MemoryStatus.CONFLICTED.value
                     if prior is not None and not can_replace
                     else MemoryStatus.ACTIVE.value
                 ),
                 str(prior["memory_id"]) if prior is not None and can_replace else None,
                 None,
                 digest, 1, now, now),
            )
            if prior is not None and can_replace:
                changed = db.execute(
                    """UPDATE memory_records SET status='superseded', superseded_by=?,
                       revision=revision+1, updated_at=?
                       WHERE memory_id=? AND revision=? AND status='active'""",
                    (memory_id, now, prior["memory_id"], prior["revision"]),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("memory supersede revision conflict")
                self._enqueue(db, str(prior["memory_id"]), int(prior["revision"]) + 1,
                              str(prior["scope"]), str(prior["scope_key"]), now)
                self._mutation(db, str(prior["memory_id"]), "superseded",
                               int(prior["revision"]), int(prior["revision"]) + 1,
                               {"superseded_by": memory_id}, now)
            elif prior is not None:
                changed = db.execute(
                    """UPDATE memory_records SET status='conflicted',revision=revision+1,updated_at=?
                       WHERE memory_id=? AND revision=? AND status='active'""",
                    (now, prior["memory_id"], prior["revision"]),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("memory conflict revision conflict")
                self._enqueue(db, str(prior["memory_id"]), int(prior["revision"]) + 1,
                              str(prior["scope"]), str(prior["scope_key"]), now)
                self._mutation(db, str(prior["memory_id"]), "conflicted",
                               int(prior["revision"]), int(prior["revision"]) + 1,
                               {"conflicts_with": memory_id}, now)
            self._append_evidence(db, memory_id, request, now)
            self._mutation(db, memory_id, "created", None, 1, request.model_dump(mode="json"), now)
            self._enqueue(db, memory_id, 1, request.scope.value, request.scope_key, now)
            row = db.execute("SELECT * FROM memory_records WHERE memory_id=?", (memory_id,)).fetchone()
            assert row is not None
            return self._row_record(row)

    def records_for_scopes(
        self, scopes: Sequence[tuple[MemoryScope, str]], *, kinds: Sequence[str] = (),
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,), limit: int = 200,
        excluded_kinds: Sequence[str] = (),
    ) -> tuple[MemoryRecord, ...]:
        if not scopes or limit <= 0:
            return ()
        scope_sql = " OR ".join("(scope=? AND scope_key=?)" for _ in scopes)
        params: list[object] = [part for pair in scopes for part in (pair[0].value, pair[1])]
        status_sql = ",".join("?" for _ in statuses)
        params.extend(item.value for item in statuses)
        kind_clause = ""
        if kinds:
            kind_clause = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        if excluded_kinds:
            kind_clause += " AND kind NOT IN (" + ",".join("?" for _ in excluded_kinds) + ")"
            params.extend(excluded_kinds)
        params.append(limit)
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT * FROM memory_records WHERE ({scope_sql}) AND status IN ({status_sql})"
                f"{kind_clause} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return tuple(self._row_record(row) for row in rows)

    def record_retrieval(self, payload: dict[str, object]) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO memory_retrievals(
                    retrieval_id,run_id,turn_id,query_hash,store_watermark,
                    allowed_scope_hashes_json,candidate_count,selected_memory_ids_json,
                    score_components_json,token_budget,used_tokens,index_version,duration_ms,
                    degradation_reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (payload["retrieval_id"], payload.get("run_id"), payload.get("turn_id"),
                 payload["query_hash"], payload["store_watermark"],
                 json.dumps(payload.get("allowed_scope_hashes", []), sort_keys=True),
                 payload["candidate_count"], json.dumps(payload.get("selected_memory_ids", [])),
                 json.dumps(payload.get("score_components", {}), sort_keys=True),
                 payload["token_budget"], payload["used_tokens"], payload.get("index_version"),
                 payload.get("duration_ms", 0.0), payload.get("degradation_reason"), utc_now()),
            )

    def record_retrieval_feedback(
        self, retrieval_id: str, *, outcome: str, selected_memory_ids: Sequence[str],
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO memory_retrieval_feedback VALUES(?,?,?,?)",
                (retrieval_id, outcome, json.dumps(list(selected_memory_ids)), utc_now()),
            )

    def health(self) -> dict[str, object]:
        with closing(self._connect()) as db:
            counts = {row[0]: int(row[1]) for row in db.execute(
                "SELECT status, COUNT(*) FROM memory_records GROUP BY status"
            ).fetchall()}
            pending = int(db.execute(
                "SELECT COUNT(*) FROM memory_index_jobs WHERE status!='ready'"
            ).fetchone()[0])
            failed = int(db.execute(
                "SELECT COUNT(*) FROM memory_index_jobs WHERE status='failed'"
            ).fetchone()[0])
            embedding_pending = int(db.execute(
                "SELECT COUNT(*) FROM memory_embedding_jobs WHERE status!='ready'"
            ).fetchone()[0])
            projection_lag = int(db.execute(
                "SELECT COUNT(*) FROM memory_projection_jobs WHERE status!='ready'"
            ).fetchone()[0])
            projection_drift = int(db.execute(
                "SELECT COUNT(*) FROM memory_projection_state WHERE status='drift'"
            ).fetchone()[0])
            latest_retrieval = db.execute(
                """SELECT duration_ms,candidate_count,selected_memory_ids_json
                   FROM memory_retrievals ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        selected_count = 0
        if latest_retrieval is not None:
            try:
                selected_count = len(json.loads(str(latest_retrieval["selected_memory_ids_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                selected_count = 0
        return {
            "memory_record_count": sum(counts.values()),
            **{f"memory_{key}_count": value for key, value in counts.items()},
            "memory_index_pending": pending,
            "memory_index_failed": failed,
            "memory_embedding_pending": embedding_pending,
            "profile_projection_lag": projection_lag,
            "profile_projection_drift": projection_drift,
            "memory_retrieval_latency_ms": (
                float(latest_retrieval["duration_ms"]) if latest_retrieval is not None else 0.0
            ),
            "memory_retrieval_candidate_count": (
                int(latest_retrieval["candidate_count"]) if latest_retrieval is not None else 0
            ),
            "memory_retrieval_selected_count": selected_count,
        }

    def compensate_source(self, source_ref_prefix: str) -> int:
        """Append compensating mutations for a failed/rolled-back producer run."""
        now = utc_now()
        changed = 0
        with self.transaction() as db:
            rows = db.execute(
                """SELECT * FROM memory_records
                   WHERE primary_source_ref LIKE ? AND status='active'""",
                (source_ref_prefix + "%",),
            ).fetchall()
            for row in rows:
                evidence = int(db.execute(
                    "SELECT COUNT(*) FROM memory_evidence WHERE memory_id=? AND source_ref NOT LIKE ?",
                    (row["memory_id"], source_ref_prefix + "%"),
                ).fetchone()[0])
                if evidence:
                    self._mutation(db, row["memory_id"], "source_compensated",
                                   row["revision"], row["revision"],
                                   {"source_ref_prefix": source_ref_prefix}, now)
                    continue
                revision = int(row["revision"]) + 1
                db.execute(
                    "UPDATE memory_records SET status='tombstoned',revision=?,updated_at=? WHERE memory_id=? AND revision=?",
                    (revision, now, row["memory_id"], row["revision"]),
                )
                self._mutation(db, row["memory_id"], "compensating_tombstone",
                               row["revision"], revision,
                               {"source_ref_prefix": source_ref_prefix}, now)
                self._enqueue(db, row["memory_id"], revision, row["scope"], row["scope_key"], now)
                changed += 1
        return changed

    def _append_evidence(self, db: sqlite3.Connection, memory_id: str,
                         request: MemoryWriteRequest, now: str) -> None:
        identity = f"{memory_id}\0{request.source}\0{request.source_ref}\0{request.locator or ''}"
        evidence_id = "evi_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        db.execute(
            """INSERT OR IGNORE INTO memory_evidence(
               evidence_id,memory_id,source,source_ref,source_hash,locator,confidence,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (evidence_id, memory_id, request.source, request.source_ref, request.source_hash,
             request.locator, request.confidence, now),
        )

    def _mutation(self, db: sqlite3.Connection, memory_id: str, kind: str,
                  before: int | None, after: int, payload: object, now: str) -> None:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        mutation_id = "mut_" + hashlib.sha256(
            f"{memory_id}\0{kind}\0{after}\0{canonical}".encode("utf-8")
        ).hexdigest()[:24]
        db.execute(
            "INSERT OR IGNORE INTO memory_mutations VALUES(?,?,?,?,?,?,?)",
            (mutation_id, memory_id, kind, before, after, canonical, now),
        )

    def _enqueue(self, db: sqlite3.Connection, memory_id: str, revision: int,
                 scope: str, scope_key: str, now: str) -> None:
        index_id = "idx_" + hashlib.sha256(f"{memory_id}:{revision}".encode()).hexdigest()[:24]
        db.execute(
            "INSERT OR IGNORE INTO memory_index_jobs VALUES(?,?,?,?,?,?,?, ?,?)",
            (index_id, memory_id, revision, "upsert", "pending", 0, None, now, now),
        )
        watermark = int(db.execute("SELECT COALESCE(MAX(rowid),0) FROM memory_mutations").fetchone()[0])
        projection_id = "prj_" + hashlib.sha256(
            f"{scope}:{scope_key}:{watermark}".encode()
        ).hexdigest()[:24]
        db.execute(
            "INSERT OR IGNORE INTO memory_projection_jobs VALUES(?,?,?,?,?,?,?,?,?)",
            (projection_id, scope, scope_key, watermark, "pending", 0, None, now, now),
        )

    def ensure_embedding_jobs(self, model: str, version: int) -> int:
        """Materialize versioned semantic projection intent after explicit opt-in."""
        now = utc_now()
        created = 0
        with self.transaction() as db:
            rows = db.execute("SELECT memory_id,revision FROM memory_records WHERE status='active'").fetchall()
            for row in rows:
                job_id = "emb_" + hashlib.sha256(
                    f"{row['memory_id']}:{row['revision']}:{model}:{version}".encode()
                ).hexdigest()[:24]
                changed = db.execute(
                    """INSERT OR IGNORE INTO memory_embedding_jobs VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, row["memory_id"], row["revision"], model, version,
                     "pending", 0, None, now, now),
                )
                created += changed.rowcount
        return created

    @staticmethod
    def _row_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"], scope=MemoryScope(row["scope"]),
            scope_key=row["scope_key"], kind=row["kind"], subject_key=row["subject_key"],
            cardinality=MemoryCardinality(row["cardinality"]), content=row["content"],
            normalized_content=row["normalized_content"], source=row["source"],
            primary_source_ref=row["primary_source_ref"], importance=float(row["importance"]),
            confidence=float(row["confidence"]), pinned=bool(row["pinned"]),
            valid_from=row["valid_from"], valid_until=row["valid_until"],
            status=MemoryStatus(row["status"]), supersedes=row["supersedes"],
            superseded_by=row["superseded_by"], content_hash=row["content_hash"],
            revision=int(row["revision"]), created_at=row["created_at"], updated_at=row["updated_at"],
        )


class MemoryWriter:
    def __init__(self, store: StructuredMemoryStore, *, supersede_confidence_threshold: float = 0.8) -> None:
        self.store = store
        self.supersede_confidence_threshold = supersede_confidence_threshold

    def write(self, request: MemoryWriteRequest) -> MemoryRecord:
        return self.store.write(request, supersede_threshold=self.supersede_confidence_threshold)


class MemoryIndexWorker:
    """Idempotent bridge from canonical jobs to a rebuildable FTS database."""

    def __init__(self, store: StructuredMemoryStore) -> None:
        self.store = store
        self.path = store.index_path
        self.initialize()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_index(
                    memory_id TEXT PRIMARY KEY, scope TEXT NOT NULL, scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL,
                    content_hash TEXT NOT NULL, source_revision INTEGER NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED, scope UNINDEXED, scope_key UNINDEXED,
                    kind UNINDEXED, content, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS memory_embeddings(
                    memory_id TEXT NOT NULL, embedding_model TEXT NOT NULL,
                    embedding_version INTEGER NOT NULL, dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(memory_id, embedding_model, embedding_version)
                );
                """)
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("memory index quick_check failed")
                db.commit()
        except sqlite3.DatabaseError:
            # index.sqlite3 is explicitly rebuildable. Canonical facts remain in
            # memory.sqlite3 and are never touched by this recovery.
            self.path.unlink(missing_ok=True)
            Path(str(self.path) + "-wal").unlink(missing_ok=True)
            Path(str(self.path) + "-shm").unlink(missing_ok=True)
            with closing(sqlite3.connect(self.path)) as db:
                db.executescript("""
                    CREATE TABLE memory_index(
                        memory_id TEXT PRIMARY KEY, scope TEXT NOT NULL, scope_key TEXT NOT NULL,
                        kind TEXT NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL,
                        content_hash TEXT NOT NULL, source_revision INTEGER NOT NULL
                    );
                    CREATE VIRTUAL TABLE memory_fts USING fts5(
                        memory_id UNINDEXED, scope UNINDEXED, scope_key UNINDEXED,
                        kind UNINDEXED, content, tokenize='unicode61'
                    );
                    CREATE TABLE memory_embeddings(
                        memory_id TEXT NOT NULL, embedding_model TEXT NOT NULL,
                        embedding_version INTEGER NOT NULL, dimensions INTEGER NOT NULL,
                        vector BLOB NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                        PRIMARY KEY(memory_id, embedding_model, embedding_version)
                    );
                """)
                db.commit()
            self.rebuild()

    def reconcile(self, *, limit: int = 100) -> int:
        processed = 0
        while processed < limit:
            with self.store.transaction() as canonical:
                job = canonical.execute(
                    """SELECT * FROM memory_index_jobs
                       WHERE status IN ('pending','failed','running')
                       ORDER BY created_at, job_id LIMIT 1"""
                ).fetchone()
                if job is None:
                    break
                canonical.execute(
                    "UPDATE memory_index_jobs SET status='running', attempts=attempts+1, updated_at=? WHERE job_id=?",
                    (utc_now(), job["job_id"]),
                )
                record = canonical.execute(
                    "SELECT * FROM memory_records WHERE memory_id=?", (job["memory_id"],)
                ).fetchone()
            try:
                if record is None:
                    raise RuntimeError("canonical memory record missing")
                with closing(sqlite3.connect(self.path)) as index:
                    index.execute("BEGIN IMMEDIATE")
                    index.execute(
                        """INSERT INTO memory_index VALUES(?,?,?,?,?,?,?,?)
                           ON CONFLICT(memory_id) DO UPDATE SET
                           scope=excluded.scope,scope_key=excluded.scope_key,kind=excluded.kind,
                           status=excluded.status,content=excluded.content,
                           content_hash=excluded.content_hash,source_revision=excluded.source_revision""",
                        (record["memory_id"], record["scope"], record["scope_key"], record["kind"],
                         record["status"], record["content"], record["content_hash"], record["revision"]),
                    )
                    index.execute("DELETE FROM memory_fts WHERE memory_id=?", (record["memory_id"],))
                    if record["status"] == "active":
                        index.execute(
                            "INSERT INTO memory_fts VALUES(?,?,?,?,?)",
                            (record["memory_id"], record["scope"], record["scope_key"],
                             record["kind"], record["content"]),
                        )
                    index.commit()
                with self.store.transaction() as canonical:
                    canonical.execute(
                        "UPDATE memory_index_jobs SET status='ready', last_error=NULL, updated_at=? WHERE job_id=?",
                        (utc_now(), job["job_id"]),
                    )
                processed += 1
            except Exception as exc:
                with self.store.transaction() as canonical:
                    canonical.execute(
                        "UPDATE memory_index_jobs SET status='failed', last_error=?, updated_at=? WHERE job_id=?",
                        (f"{type(exc).__name__}: {str(exc)[:500]}", utc_now(), job["job_id"]),
                    )
                break
        return processed

    def rebuild(self) -> int:
        self.path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        self.initialize()
        now = utc_now()
        with self.store.transaction() as db:
            rows = db.execute("SELECT memory_id, revision FROM memory_records").fetchall()
            for row in rows:
                job_id = "idx_" + hashlib.sha256(
                    f"{row['memory_id']}:{row['revision']}:rebuild".encode()
                ).hexdigest()[:24]
                db.execute(
                    "INSERT OR IGNORE INTO memory_index_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                    (job_id, row["memory_id"], row["revision"], "upsert", "pending", 0, None, now, now),
                )
        return self.reconcile(limit=max(100, len(rows) + 1))


class MemoryProfileProjector:
    """Human-readable projection.  It never feeds facts back into runtime recall."""

    def __init__(self, store: StructuredMemoryStore) -> None:
        self.store = store

    def reconcile(self, *, limit: int = 100) -> int:
        processed = 0
        with closing(self.store._connect()) as db:
            jobs = db.execute(
                "SELECT * FROM memory_projection_jobs WHERE status IN ('pending','failed','running') ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        for job in jobs:
            try:
                scope = MemoryScope(job["scope"])
                records = self.store.records_for_scopes(((scope, job["scope_key"]),), limit=10000)
                projections = self._projections(scope, str(job["scope_key"]), records)
                rendered: list[tuple[str, Path, str, str]] = []
                drifted: list[str] = []
                with closing(self.store._connect()) as state_db:
                    for projection_id, path, selected in projections:
                        body = self._render(scope, selected)
                        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                        state = state_db.execute(
                            "SELECT * FROM memory_projection_state WHERE projection_id=?",
                            (projection_id,),
                        ).fetchone()
                        if state is not None and path.exists():
                            current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                            if current_hash not in {state["content_hash"], digest}:
                                drifted.append(projection_id)
                        rendered.append((projection_id, path, body, digest))
                if drifted:
                    with self.store.transaction() as state_db:
                        for projection_id in drifted:
                            state_db.execute(
                                "UPDATE memory_projection_state SET status='drift',updated_at=? WHERE projection_id=?",
                                (utc_now(), projection_id),
                            )
                        state_db.execute(
                            "UPDATE memory_projection_jobs SET status='failed',last_error='profile_projection_drift',updated_at=? WHERE job_id=?",
                            (utc_now(), job["job_id"]),
                        )
                    continue
                for _, path, body, _ in rendered:
                    temporary = path.with_suffix(path.suffix + ".tmp")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                        handle.write(body)
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary.replace(path)
                    self._fsync_directory(path.parent)
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE memory_projection_jobs SET status='ready',last_error=NULL,updated_at=? WHERE job_id=?",
                        (utc_now(), job["job_id"]),
                    )
                    for projection_id, _, _, digest in rendered:
                        db.execute(
                            """INSERT INTO memory_projection_state VALUES(?,?,?,?,?)
                               ON CONFLICT(projection_id) DO UPDATE SET watermark=excluded.watermark,
                               content_hash=excluded.content_hash,status=excluded.status,updated_at=excluded.updated_at""",
                            (projection_id, job["target_watermark"], digest, "ready", utc_now()),
                        )
                processed += 1
            except Exception as exc:
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE memory_projection_jobs SET status='failed',last_error=?,updated_at=? WHERE job_id=?",
                        (f"{type(exc).__name__}: {str(exc)[:500]}", utc_now(), job["job_id"]),
                    )
        return processed

    @staticmethod
    def _render(scope: MemoryScope, records: Sequence[MemoryRecord]) -> str:
        lines = [f"# {scope.value.title()} Memory", "", "<!-- generated from memory.sqlite3; do not edit -->", ""]
        for record in records:
            lines.extend((f"## {record.kind}", "", record.content, "",
                          f"<!-- memory_id={record.memory_id} revision={record.revision} -->", ""))
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if hasattr(os, "O_DIRECTORY"):
            try:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                pass

    def _projections(
        self, scope: MemoryScope, scope_key: str, records: Sequence[MemoryRecord],
    ) -> tuple[tuple[str, Path, tuple[MemoryRecord, ...]], ...]:
        safe = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:16]
        if self.store.root.parent.name == "harness-evolution" and scope is MemoryScope.HARNESS:
            return (
                (f"{scope.value}:{scope_key}:changes", self.store.profile_root / "CHANGES.md",
                 tuple(item for item in records if item.kind != "lesson")),
                (f"{scope.value}:{scope_key}:lessons", self.store.profile_root / "LESSONS.md",
                 tuple(item for item in records if item.kind == "lesson")),
            )
        if scope is MemoryScope.USER:
            groups = (
                ("user", "USER.md", {"profile", "preference", "constraint", "decision"}),
                ("research", "RESEARCH.md", {"research"}),
                ("others", "OTHERS.md", {"other"}),
            )
            return tuple(
                (f"{scope.value}:{scope_key}:{name}", self.store.profile_root / filename,
                 tuple(item for item in records if item.kind in kinds))
                for name, filename, kinds in groups
            )
        if scope is MemoryScope.PROJECT:
            path = self.store.profile_root / f"PROJECT-{safe}.md"
            return ((f"{scope.value}:{scope_key}", path, tuple(records)),)
        if scope is MemoryScope.SESSION:
            path = self.store.profile_root / f"{scope_key}.md"
            return ((f"{scope.value}:{scope_key}", path, tuple(records)),)
        if scope is MemoryScope.HARNESS:
            path = self.store.profile_root / f"HARNESS-{safe}.md"
            return ((f"{scope.value}:{scope_key}", path, tuple(records)),)
        path = self.store.profile_root / f"RUN-{safe}.md"
        return ((f"{scope.value}:{scope_key}", path, tuple(records)),)


class LegacyMemoryMigrator:
    """One-way import of legacy Dream/Profile facts into the canonical store."""

    version = 1

    def __init__(self, store: StructuredMemoryStore, *, project_key: str,
                 user_key: str = "local-user") -> None:
        self.store = store
        self.writer = MemoryWriter(store)
        self.project_key = project_key
        self.user_key = user_key

    def migrate(self) -> dict[str, int]:
        totals = {"validated": 0, "imported": 0, "deduplicated": 0}
        dream = self.store.root.parent / "dream" / "memories.json"
        if dream.is_file():
            self._run_source(dream, self._migrate_dream, totals)
        if self.store.profile_root.is_dir():
            for path in sorted(self.store.profile_root.glob("*.md")):
                if not path.is_symlink():
                    self._run_source(path, self._migrate_markdown, totals)
        return totals

    def _run_source(self, path: Path, migrate, totals: dict[str, int]) -> None:
        try:
            migrate(path, totals)
        except Exception:
            # Preserve the source and durable migration evidence. A later
            # startup may retry the same source hash; no imported canonical
            # facts are deleted or silently skipped.
            with self.store.transaction() as db:
                db.execute(
                    """UPDATE memory_migrations SET status='failed',completed_at=?
                       WHERE migration_version=? AND source_path=? AND status='running'""",
                    (utc_now(), self.version, str(path.resolve())),
                )
            raise

    def _already_migrated(self, path: Path) -> bool:
        with closing(self.store._connect()) as db:
            return db.execute(
                "SELECT 1 FROM memory_migrations WHERE migration_version=? AND source_path=? AND status='completed'",
                (self.version, str(path.resolve())),
            ).fetchone() is not None

    def _begin(self, path: Path) -> tuple[str, str] | None:
        if self._already_migrated(path):
            return None
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        migration_id = "mmig_" + hashlib.sha256(
            f"{self.version}:{path.resolve()}:{source_hash}".encode()
        ).hexdigest()[:24]
        with self.store.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO memory_migrations VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (migration_id, self.version, str(path.resolve()), source_hash, 0, 0, 0,
                 "running", utc_now(), None),
            )
        return migration_id, source_hash

    def _complete(self, migration_id: str, validated: int, imported: int, deduped: int) -> None:
        with self.store.transaction() as db:
            db.execute(
                """UPDATE memory_migrations SET validated_count=?, imported_count=?,
                   deduplicated_count=?, status='completed', completed_at=? WHERE migration_id=?""",
                (validated, imported, deduped, utc_now(), migration_id),
            )

    def _migrate_dream(self, path: Path, totals: dict[str, int]) -> None:
        started = self._begin(path)
        if started is None:
            return
        migration_id, source_hash = started
        payload = json.loads(path.read_text(encoding="utf-8"))
        memories = payload.get("memories", {}) if isinstance(payload, dict) else {}
        if not isinstance(memories, dict):
            raise ValueError(f"legacy Dream memory must contain an object: {path}")
        validated = imported = deduped = 0
        for identity, value in memories.items():
            if not isinstance(value, dict) or value.get("status", "active") != "active":
                continue
            statement = value.get("statement")
            target = str(value.get("target_file", "USER.md"))
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError(f"invalid legacy Dream memory {identity}")
            validated += 1
            before = self._count()
            self.writer.write(MemoryWriteRequest(
                scope=self._scope_for(target)[0], scope_key=self._scope_for(target)[1],
                kind=self._kind_for(target), content=_strip_legacy_markup(statement),
                source="legacy_import", source_ref=str(path.resolve()),
                confidence=float(value.get("confidence", 0.6)),
                source_hash=source_hash, locator=f"memory:{identity}",
            ))
            if self._count() > before:
                imported += 1
            else:
                deduped += 1
        self._complete(migration_id, validated, imported, deduped)
        _add_totals(totals, validated, imported, deduped)

    def _migrate_markdown(self, path: Path, totals: dict[str, int]) -> None:
        started = self._begin(path)
        if started is None:
            return
        migration_id, source_hash = started
        text = path.read_text(encoding="utf-8")
        is_harness = self.store.root.parent.name == "harness-evolution"
        if is_harness and path.name == "AGENT.md":
            # AGENT.md is a stable execution rule, not recalled long-term fact.
            self._complete(migration_id, 0, 0, 0)
            return
        if "generated from memory.sqlite3" in text:
            self._complete(migration_id, 0, 0, 0)
            return
        statements = _legacy_markdown_statements(text)
        validated = imported = deduped = 0
        scope, key = self._scope_for(path.name)
        for line_no, statement in statements:
            validated += 1
            before = self._count()
            self.writer.write(MemoryWriteRequest(
                scope=scope, scope_key=key, kind=self._kind_for(path.name), content=statement,
                source="legacy_import", source_ref=str(path.resolve()), confidence=0.6,
                source_hash=source_hash, locator=f"line:{line_no}",
            ))
            if self._count() > before:
                imported += 1
            else:
                deduped += 1
        self._complete(migration_id, validated, imported, deduped)
        _add_totals(totals, validated, imported, deduped)

    def _scope_for(self, name: str) -> tuple[MemoryScope, str]:
        upper = name.upper()
        if self.store.root.parent.name == "harness-evolution" and upper in {
            "PROJECT.MD", "CHANGES.MD", "LESSONS.MD",
        }:
            return MemoryScope.HARNESS, self.project_key
        if upper.startswith("PROJECT"):
            return MemoryScope.PROJECT, self.project_key
        if re.fullmatch(r"[0-9a-f]{16}\.md", name):
            return MemoryScope.SESSION, Path(name).stem
        return MemoryScope.USER, self.user_key

    @staticmethod
    def _kind_for(name: str) -> str:
        upper = name.upper()
        if upper == "RESEARCH.MD":
            return "research"
        if upper == "OTHERS.MD":
            return "other"
        if upper == "LESSONS.MD":
            return "lesson"
        if upper == "CHANGES.MD":
            return "verified_change"
        return "profile"

    def _count(self) -> int:
        with closing(self.store._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0])


def _strip_legacy_markup(value: str) -> str:
    selected = re.sub(r"<!--.*?-->", "", value.strip())
    return re.sub(r"^[-*+]\s+", "", selected).strip()


def _legacy_markdown_statements(value: str) -> tuple[tuple[int, str], ...]:
    statements: list[tuple[int, str]] = []
    for line_no, line in enumerate(value.splitlines(), 1):
        selected = line.strip()
        if not selected or selected.startswith(("#", "<!--", "```")):
            continue
        selected = _strip_legacy_markup(selected)
        if selected:
            statements.append((line_no, selected))
    return tuple(statements)


def _add_totals(target: dict[str, int], validated: int, imported: int, deduped: int) -> None:
    target["validated"] += validated
    target["imported"] += imported
    target["deduplicated"] += deduped
