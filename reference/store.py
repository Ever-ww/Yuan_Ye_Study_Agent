"""全局论文资料库的 SQLite 存储、全文索引与向量任务队列。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    Author,
    CitationExample,
    CitationExampleCreate,
    EmbeddingJob,
    Paper,
    PaperFile,
    PaperIdentifier,
    PaperUpsert,
    ReferenceSearchHit,
    ReferenceSearchRequest,
    SourcePassage,
    SourcePassageCreate,
)


SCHEMA_VERSION = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReferenceStore:
    """线程安全、可迁移且跨 workspace 共享的 Reference 数据库。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.directory = self.database_path.parent
        self.backups_directory = self.directory / "backups"
        self.migration_backup_path: Path | None = None
        self._lock = threading.RLock()
        self.fts_available = False
        self.initialize()

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._backup_before_migration()
        with self._lock, self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Reference 数据库版本 {version} 高于程序支持版本 {SCHEMA_VERSION}")
            check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if check != "ok":
                raise RuntimeError(f"Reference SQLite quick_check 失败：{check}")
            connection.executescript(_SCHEMA)
            self._migrate(connection)
            try:
                connection.execute(_FTS_SCHEMA)
                connection.executescript(_FTS_TRIGGERS)
                document_count = int(connection.execute("SELECT count(*) FROM search_documents").fetchone()[0])
                fts_count = int(connection.execute("SELECT count(*) FROM search_documents_fts").fetchone()[0])
                if document_count != fts_count:
                    connection.execute("INSERT INTO search_documents_fts(search_documents_fts) VALUES('rebuild')")
                self.fts_available = True
            except sqlite3.OperationalError as exc:
                if "fts5" not in str(exc).lower():
                    raise
                self.fts_available = False
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "UPDATE embedding_jobs SET status='pending',updated_at=? WHERE status='running'",
                (now_iso(),),
            )
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError("Reference SQLite foreign_key_check 失败")

    def upsert_paper(self, value: PaperUpsert) -> Paper:
        timestamp = now_iso()
        normalized_title = _normalize_text(value.title)
        with self._lock, self._connect() as connection:
            paper_id = self._find_paper_id(connection, value, normalized_title)
            if paper_id is None:
                paper_id = uuid4().hex
                connection.execute(
                    "INSERT INTO papers("
                    "paper_id,title,normalized_title,abstract,publication_year,publication_date,"
                    "language,venue,publisher,license,canonical_url,pdf_url,citation_key,status,"
                    "metadata_json,source_session_id,source_workspace,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        paper_id, value.title.strip(), normalized_title, value.abstract.strip(),
                        value.publication_year, value.publication_date, value.language.strip(),
                        value.venue.strip(), value.publisher.strip(), value.license.strip(),
                        value.canonical_url, value.pdf_url, value.citation_key, "active",
                        _json(value.metadata), value.source_session_id, value.source_workspace,
                        timestamp, timestamp,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE papers SET title=?,normalized_title=?,abstract=?,publication_year=?,"
                    "publication_date=?,language=?,venue=?,publisher=?,license=?,canonical_url=?,"
                    "pdf_url=?,citation_key=?,metadata_json=?,source_session_id=COALESCE(?,source_session_id),"
                    "source_workspace=COALESCE(?,source_workspace),updated_at=? WHERE paper_id=?",
                    (
                        value.title.strip(), normalized_title, value.abstract.strip(), value.publication_year,
                        value.publication_date, value.language.strip(), value.venue.strip(),
                        value.publisher.strip(), value.license.strip(), value.canonical_url,
                        value.pdf_url, value.citation_key, _json(value.metadata), value.source_session_id,
                        value.source_workspace, timestamp, paper_id,
                    ),
                )
            for identifier in value.identifiers:
                connection.execute(
                    "INSERT INTO paper_identifiers(identifier_id,paper_id,scheme,value,normalized_value) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(scheme,normalized_value) DO UPDATE SET "
                    "paper_id=excluded.paper_id,value=excluded.value",
                    (uuid4().hex, paper_id, identifier.scheme.lower(), identifier.value, _normalize_identifier(identifier)),
                )
            connection.execute("DELETE FROM paper_authors WHERE paper_id=?", (paper_id,))
            for position, author in enumerate(value.authors, 1):
                author_id = self._upsert_author(connection, author)
                connection.execute(
                    "INSERT INTO paper_authors(paper_id,author_id,position,role,affiliation) VALUES(?,?,?,?,?)",
                    (paper_id, author_id, position, "author", author.affiliation),
                )
            for tag in value.tags:
                clean = tag.strip()
                if clean:
                    tag_id = hashlib.sha256(clean.casefold().encode()).hexdigest()[:24]
                    connection.execute("INSERT OR IGNORE INTO tags(tag_id,name) VALUES(?,?)", (tag_id, clean))
                    connection.execute("INSERT OR IGNORE INTO paper_tags(paper_id,tag_id) VALUES(?,?)", (paper_id, tag_id))
            self._refresh_paper_document(connection, paper_id)
        return self.get_paper(paper_id)

    def _backup_before_migration(self) -> None:
        """在修改旧 Reference 库前创建 SQLite 一致性备份。"""
        if not self.database_path.is_file() or self.database_path.stat().st_size == 0:
            return
        with sqlite3.connect(self.database_path, timeout=30) as source:
            tables = source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            ).fetchall()
            if not tables:
                return
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            paper_columns = {
                str(row[1]) for row in source.execute("PRAGMA table_info(papers)").fetchall()
            }
            needs_migration = version < SCHEMA_VERSION or not {
                "source_session_id", "source_workspace",
            }.issubset(paper_columns)
            if not needs_migration:
                return
            check = str(source.execute("PRAGMA quick_check").fetchone()[0])
            if check != "ok":
                raise RuntimeError(f"Reference SQLite quick_check 失败：{check}")
            self.backups_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.backups_directory / (
                f"reference-v{version}-to-v{SCHEMA_VERSION}-{stamp}-{uuid4().hex[:8]}.sqlite3"
            )
            with sqlite3.connect(backup) as target:
                source.backup(target)
            self.migration_backup_path = backup

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """修复早期 v1 papers 表缺少来源字段但版本号未变的情况。"""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(papers)").fetchall()
        }
        if "source_session_id" not in columns:
            connection.execute("ALTER TABLE papers ADD COLUMN source_session_id TEXT")
        if "source_workspace" not in columns:
            connection.execute("ALTER TABLE papers ADD COLUMN source_workspace TEXT")

    def add_file(self, paper_id: str, value: PaperFile) -> PaperFile:
        with self._lock, self._connect() as connection:
            self._require_paper(connection, paper_id)
            if value.is_primary:
                connection.execute("UPDATE paper_files SET is_primary=0 WHERE paper_id=?", (paper_id,))
            connection.execute(
                "INSERT INTO paper_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(paper_id,sha256) DO UPDATE SET workspace_hash=excluded.workspace_hash,"
                "workspace_root=excluded.workspace_root,relative_path=excluded.relative_path,"
                "absolute_path=excluded.absolute_path,mime_type=excluded.mime_type,"
                "size_bytes=excluded.size_bytes,is_primary=excluded.is_primary,source_session_id=excluded.source_session_id",
                (
                    uuid4().hex, paper_id, value.workspace_hash, value.workspace_root,
                    value.relative_path, value.absolute_path, value.sha256, value.mime_type,
                    value.size_bytes, int(value.is_primary), value.source_session_id, now_iso(),
                ),
            )
        return value

    def add_passage(self, value: SourcePassageCreate) -> SourcePassage:
        timestamp = now_iso()
        text_hash = hashlib.sha256(value.text.strip().encode()).hexdigest()
        passage_id = uuid4().hex
        with self._lock, self._connect() as connection:
            self._require_paper(connection, value.paper_id)
            existing = connection.execute(
                "SELECT passage_id FROM source_passages WHERE paper_id=? AND text_hash=? AND "
                "COALESCE(page_start,0)=COALESCE(?,0) AND section=?",
                (value.paper_id, text_hash, value.page_start, value.section),
            ).fetchone()
            if existing is not None:
                return self.get_passage(str(existing["passage_id"]))
            connection.execute(
                "INSERT INTO source_passages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    passage_id, value.paper_id, value.text.strip(), text_hash,
                    value.context_before, value.context_after, value.page_start, value.page_end,
                    value.section, value.paragraph, value.language, value.translation,
                    value.extraction_method, value.verification_status, value.source_session_id,
                    value.source_workspace, _json(value.locator), timestamp, timestamp,
                ),
            )
            self._upsert_search_document(connection, "passage", passage_id, value.paper_id)
        return self.get_passage(passage_id)

    def add_citation_example(self, value: CitationExampleCreate) -> CitationExample:
        timestamp = now_iso()
        example_id = uuid4().hex
        with self._lock, self._connect() as connection:
            self._require_paper(connection, value.paper_id)
            connection.execute(
                "INSERT INTO citation_examples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    example_id, value.paper_id, value.text.strip(), value.language,
                    value.citation_style, value.claim, value.note, value.created_by,
                    value.verification_status, value.source_session_id, value.source_workspace,
                    timestamp, timestamp,
                ),
            )
            for position, passage_id in enumerate(value.source_passage_ids, 1):
                row = connection.execute(
                    "SELECT paper_id FROM source_passages WHERE passage_id=?", (passage_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"未知原文摘录：{passage_id}")
                connection.execute(
                    "INSERT INTO citation_example_sources(example_id,paper_id,passage_id,position,relation_type) "
                    "VALUES(?,?,?,?,?)",
                    (example_id, str(row["paper_id"]), passage_id, position, "supports"),
                )
            self._upsert_search_document(connection, "citation_example", example_id, value.paper_id)
        return self.get_citation_example(example_id)

    def archive(self, paper_id: str, archived: bool = True) -> Paper:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE papers SET status=?,updated_at=? WHERE paper_id=?",
                ("archived" if archived else "active", now_iso(), paper_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"未知论文：{paper_id}")
        return self.get_paper(paper_id)

    def get_paper(self, paper_id: str) -> Paper:
        with self._connect() as connection:
            row = self._require_paper(connection, paper_id)
            identifiers = tuple(PaperIdentifier(scheme=item["scheme"], value=item["value"]) for item in connection.execute(
                "SELECT scheme,value FROM paper_identifiers WHERE paper_id=? ORDER BY scheme", (paper_id,),
            ))
            authors = tuple(Author(
                display_name=item["display_name"], given_name=item["given_name"],
                family_name=item["family_name"], orcid=item["orcid"], affiliation=item["affiliation"],
            ) for item in connection.execute(
                "SELECT a.*,pa.affiliation FROM paper_authors pa JOIN authors a USING(author_id) "
                "WHERE pa.paper_id=? ORDER BY pa.position", (paper_id,),
            ))
            files = tuple(PaperFile(
                workspace_hash=item["workspace_hash"], workspace_root=item["workspace_root"],
                relative_path=item["relative_path"], absolute_path=item["absolute_path"],
                sha256=item["sha256"], mime_type=item["mime_type"], size_bytes=item["size_bytes"],
                is_primary=bool(item["is_primary"]),
                source_session_id=item["source_session_id"],
            ) for item in connection.execute("SELECT * FROM paper_files WHERE paper_id=? ORDER BY is_primary DESC,added_at", (paper_id,)))
            tags = tuple(str(item["name"]) for item in connection.execute(
                "SELECT t.name FROM paper_tags pt JOIN tags t USING(tag_id) WHERE pt.paper_id=? ORDER BY t.name", (paper_id,),
            ))
        return Paper(
            paper_id=paper_id, title=row["title"], abstract=row["abstract"],
            publication_year=row["publication_year"], publication_date=row["publication_date"],
            language=row["language"], venue=row["venue"], publisher=row["publisher"],
            license=row["license"], canonical_url=row["canonical_url"], pdf_url=row["pdf_url"],
            citation_key=row["citation_key"], status=row["status"], metadata=json.loads(row["metadata_json"]),
            identifiers=identifiers, authors=authors, files=files, tags=tags,
            source_session_id=row["source_session_id"], source_workspace=row["source_workspace"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get_passage(self, passage_id: str) -> SourcePassage:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM source_passages WHERE passage_id=?", (passage_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知原文摘录：{passage_id}")
        return SourcePassage(
            passage_id=passage_id, paper_id=row["paper_id"], text=row["text"], text_hash=row["text_hash"],
            context_before=row["context_before"], context_after=row["context_after"], page_start=row["page_start"],
            page_end=row["page_end"], section=row["section"], paragraph=row["paragraph"], language=row["language"],
            translation=row["translation"], extraction_method=row["extraction_method"],
            verification_status=row["verification_status"], source_session_id=row["source_session_id"],
            source_workspace=row["source_workspace"], locator=json.loads(row["locator_json"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get_citation_example(self, example_id: str) -> CitationExample:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM citation_examples WHERE example_id=?", (example_id,)).fetchone()
            sources = tuple(str(item["passage_id"]) for item in connection.execute(
                "SELECT passage_id FROM citation_example_sources WHERE example_id=? AND passage_id IS NOT NULL ORDER BY position",
                (example_id,),
            )) if row is not None else ()
        if row is None:
            raise KeyError(f"未知引用例句：{example_id}")
        return CitationExample(
            example_id=example_id, paper_id=row["paper_id"], text=row["text"], language=row["language"],
            citation_style=row["citation_style"], claim=row["claim"], note=row["note"], created_by=row["created_by"],
            verification_status=row["verification_status"], source_passage_ids=sources,
            source_session_id=row["source_session_id"], source_workspace=row["source_workspace"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get_bundle(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        if entity_type == "paper":
            paper = self.get_paper(entity_id)
            with self._connect() as connection:
                passages = [self.get_passage(str(row[0])).model_dump(mode="json") for row in connection.execute(
                    "SELECT passage_id FROM source_passages WHERE paper_id=? ORDER BY created_at", (entity_id,),
                )]
                examples = [self.get_citation_example(str(row[0])).model_dump(mode="json") for row in connection.execute(
                    "SELECT example_id FROM citation_examples WHERE paper_id=? ORDER BY created_at", (entity_id,),
                )]
            return {"paper": paper.model_dump(mode="json"), "passages": passages, "citation_examples": examples}
        if entity_type == "passage":
            return self.get_passage(entity_id).model_dump(mode="json")
        if entity_type == "citation_example":
            return self.get_citation_example(entity_id).model_dump(mode="json")
        raise ValueError(f"未知 Reference 实体类型：{entity_type}")

    def lexical_search(self, request: ReferenceSearchRequest, limit: int | None = None) -> list[ReferenceSearchHit]:
        selected_limit = limit or max(request.top_k * 3, 20)
        filters, parameters = self._search_filters(request)
        with self._connect() as connection:
            if self.fts_available:
                try:
                    rows = connection.execute(
                        "SELECT sd.*,bm25(search_documents_fts) AS rank FROM search_documents_fts f "
                        "JOIN search_documents sd ON sd.rowid=f.rowid JOIN papers p ON p.paper_id=sd.paper_id "
                        "WHERE search_documents_fts MATCH ? AND p.status='active'" + filters +
                        " ORDER BY rank LIMIT ?",
                        (_fts_query(request.query), *parameters, selected_limit),
                    ).fetchall()
                    return [self._hit(row, lexical=1.0 / rank) for rank, row in enumerate(rows, 1)]
                except sqlite3.OperationalError:
                    pass
            pattern = f"%{request.query.casefold()}%"
            rows = connection.execute(
                "SELECT sd.*,0.0 AS rank FROM search_documents sd JOIN papers p ON p.paper_id=sd.paper_id "
                "WHERE p.status='active' AND lower(sd.search_text) LIKE ?" + filters + " LIMIT ?",
                (pattern, *parameters, selected_limit),
            ).fetchall()
        return [self._hit(row, lexical=1.0) for row in rows]

    def documents_for_embeddings(self, request: ReferenceSearchRequest) -> list[dict[str, Any]]:
        filters, parameters = self._search_filters(request)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sd.*,e.model,e.dimensions,e.vector,e.content_hash AS embedding_hash FROM search_documents sd "
                "JOIN papers p ON p.paper_id=sd.paper_id JOIN embeddings e ON e.document_id=sd.document_id "
                "WHERE p.status='active'" + filters,
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows if row["embedding_hash"] == row["content_hash"]]

    def search_document(self, document_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM search_documents WHERE document_id=?", (document_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知检索文档：{document_id}")
        return dict(row)

    def queue_embedding(self, document_id: str, model: str, *, force: bool = False) -> bool:
        document = self.search_document(document_id)
        timestamp = now_iso()
        with self._connect() as connection:
            if not force:
                current = connection.execute(
                    "SELECT 1 FROM embeddings WHERE document_id=? AND model=? AND content_hash=?",
                    (document_id, model, document["content_hash"]),
                ).fetchone()
                if current is not None:
                    return False
            connection.execute(
                "INSERT INTO embedding_jobs VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(document_id,model) DO UPDATE SET content_hash=excluded.content_hash,status='pending',"
                "attempts=0,next_attempt_at=NULL,last_error=NULL,updated_at=excluded.updated_at",
                (uuid4().hex, document_id, model, document["content_hash"], "pending", 0, None, None, timestamp, timestamp),
            )
        return True

    def queue_all_embeddings(self, model: str) -> int:
        with self._connect() as connection:
            ids = [str(row[0]) for row in connection.execute("SELECT document_id FROM search_documents")]
        for document_id in ids:
            self.queue_embedding(document_id, model, force=True)
        return len(ids)

    def claim_embedding_job(self, model: str) -> EmbeddingJob | None:
        timestamp = now_iso()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM embedding_jobs WHERE model=? AND status='pending' AND "
                "(next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at LIMIT 1",
                (model, timestamp),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE embedding_jobs SET status='running',updated_at=? WHERE job_id=?", (timestamp, row["job_id"]))
            data = dict(row)
            data["status"] = "running"
            data["updated_at"] = timestamp
        return EmbeddingJob.model_validate(data, strict=True)

    def complete_embedding(self, job: EmbeddingJob, vector: bytes, dimensions: int) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO embeddings(document_id,model,dimensions,vector,content_hash,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(document_id,model) DO UPDATE SET dimensions=excluded.dimensions,"
                "vector=excluded.vector,content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (job.document_id, job.model, dimensions, vector, job.content_hash, timestamp, timestamp),
            )
            connection.execute("UPDATE embedding_jobs SET status='completed',last_error=NULL,updated_at=? WHERE job_id=?", (timestamp, job.job_id))

    def fail_embedding(self, job: EmbeddingJob, error: str) -> None:
        attempts = job.attempts + 1
        timestamp = now_iso()
        status = "failed" if attempts >= 5 else "pending"
        next_attempt = None
        if status == "pending":
            next_attempt = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + 2 ** attempts,
                tz=timezone.utc,
            ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "UPDATE embedding_jobs SET status=?,attempts=?,next_attempt_at=?,last_error=?,updated_at=? WHERE job_id=?",
                (status, attempts, next_attempt, error[:1000], timestamp, job.job_id),
            )

    def pending_document_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            return tuple(str(row[0]) for row in connection.execute(
                "SELECT document_id FROM embedding_jobs WHERE status IN ('pending','running')",
            ))

    def document_ids_for_paper(self, paper_id: str) -> tuple[str, ...]:
        with self._connect() as connection:
            return tuple(str(row[0]) for row in connection.execute(
                "SELECT document_id FROM search_documents WHERE paper_id=?", (paper_id,),
            ))

    def _find_paper_id(self, connection: sqlite3.Connection, value: PaperUpsert, normalized_title: str) -> str | None:
        for identifier in value.identifiers:
            if identifier.scheme.casefold() in {"doi", "arxiv"}:
                row = connection.execute(
                    "SELECT paper_id FROM paper_identifiers WHERE scheme=? AND normalized_value=?",
                    (identifier.scheme.casefold(), _normalize_identifier(identifier)),
                ).fetchone()
                if row is not None:
                    return str(row["paper_id"])
        row = connection.execute(
            "SELECT paper_id FROM papers WHERE normalized_title=? AND COALESCE(publication_year,0)=COALESCE(?,0)",
            (normalized_title, value.publication_year),
        ).fetchone()
        return str(row["paper_id"]) if row is not None else None

    def _upsert_author(self, connection: sqlite3.Connection, value: Author) -> str:
        key = value.orcid or _normalize_text(value.display_name)
        row = connection.execute("SELECT author_id FROM authors WHERE identity_key=?", (key,)).fetchone()
        author_id = str(row["author_id"]) if row is not None else uuid4().hex
        connection.execute(
            "INSERT INTO authors VALUES(?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET "
            "display_name=excluded.display_name,given_name=excluded.given_name,family_name=excluded.family_name,orcid=excluded.orcid",
            (author_id, key, value.display_name, value.given_name, value.family_name, value.orcid),
        )
        return author_id

    def _refresh_paper_document(self, connection: sqlite3.Connection, paper_id: str) -> None:
        self._upsert_search_document(connection, "paper", paper_id, paper_id)
        for row in connection.execute("SELECT passage_id FROM source_passages WHERE paper_id=?", (paper_id,)):
            self._upsert_search_document(connection, "passage", str(row[0]), paper_id)
        for row in connection.execute("SELECT example_id FROM citation_examples WHERE paper_id=?", (paper_id,)):
            self._upsert_search_document(connection, "citation_example", str(row[0]), paper_id)

    def _upsert_search_document(self, connection: sqlite3.Connection, entity_type: str, entity_id: str, paper_id: str) -> None:
        paper = connection.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        if paper is None:
            raise KeyError(f"未知论文：{paper_id}")
        authors = " ".join(str(row[0]) for row in connection.execute(
            "SELECT a.display_name FROM paper_authors pa JOIN authors a USING(author_id) WHERE pa.paper_id=? ORDER BY pa.position",
            (paper_id,),
        ))
        keywords = " ".join(str(row[0]) for row in connection.execute(
            "SELECT t.name FROM paper_tags pt JOIN tags t USING(tag_id) WHERE pt.paper_id=?", (paper_id,),
        ))
        body = ""
        language = paper["language"]
        if entity_type == "passage":
            row = connection.execute("SELECT text,context_before,context_after,language FROM source_passages WHERE passage_id=?", (entity_id,)).fetchone()
            body = "\n".join(filter(None, (row["context_before"], row["text"], row["context_after"])))
            language = row["language"] or language
        elif entity_type == "citation_example":
            row = connection.execute("SELECT text,claim,note,language FROM citation_examples WHERE example_id=?", (entity_id,)).fetchone()
            body = "\n".join(filter(None, (row["text"], row["claim"], row["note"])))
            language = row["language"] or language
        search_text = "\n".join(filter(None, (paper["title"], paper["abstract"], authors, keywords, body)))
        content_hash = hashlib.sha256(search_text.encode()).hexdigest()
        document_id = f"{entity_type}:{entity_id}"
        connection.execute(
            "INSERT INTO search_documents(document_id,entity_type,entity_id,paper_id,title,abstract,authors,keywords,body,"
            "search_text,language,publication_year,content_hash,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,abstract=excluded.abstract,authors=excluded.authors,"
            "keywords=excluded.keywords,body=excluded.body,search_text=excluded.search_text,language=excluded.language,"
            "publication_year=excluded.publication_year,content_hash=excluded.content_hash,updated_at=excluded.updated_at",
            (document_id, entity_type, entity_id, paper_id, paper["title"], paper["abstract"], authors,
             keywords, body, search_text, language, paper["publication_year"], content_hash, now_iso()),
        )

    def _search_filters(self, request: ReferenceSearchRequest) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if request.entity_types:
            clauses.append("sd.entity_type IN (" + ",".join("?" for _ in request.entity_types) + ")")
            parameters.extend(request.entity_types)
        if request.author:
            clauses.append("lower(sd.authors) LIKE ?")
            parameters.append(f"%{request.author.casefold()}%")
        if request.year_from is not None:
            clauses.append("sd.publication_year>=?")
            parameters.append(request.year_from)
        if request.year_to is not None:
            clauses.append("sd.publication_year<=?")
            parameters.append(request.year_to)
        if request.tag:
            clauses.append("EXISTS(SELECT 1 FROM paper_tags pt JOIN tags t USING(tag_id) WHERE pt.paper_id=sd.paper_id AND lower(t.name)=?)")
            parameters.append(request.tag.casefold())
        if request.language:
            clauses.append("lower(sd.language)=?")
            parameters.append(request.language.casefold())
        return (" AND " + " AND ".join(clauses)) if clauses else "", parameters

    @staticmethod
    def _hit(row: sqlite3.Row | dict[str, Any], *, lexical: float | None = None, semantic: float | None = None) -> ReferenceSearchHit:
        return ReferenceSearchHit(
            document_id=row["document_id"], entity_type=row["entity_type"], entity_id=row["entity_id"],
            paper_id=row["paper_id"], title=row["title"], text=row["body"] or row["abstract"] or row["title"],
            lexical_score=lexical, semantic_score=semantic,
        )

    @staticmethod
    def _require_paper(connection: sqlite3.Connection, paper_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知论文：{paper_id}")
        return row

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


class _ClosingConnection(sqlite3.Connection):
    """sqlite3 的上下文管理默认不关闭句柄；Windows 下必须显式关闭。"""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _normalize_identifier(value: PaperIdentifier) -> str:
    cleaned = value.value.strip().casefold()
    if value.scheme.casefold() == "doi":
        cleaned = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", cleaned)
    if value.scheme.casefold() == "arxiv":
        cleaned = re.sub(r"^(https?://arxiv\.org/(abs|pdf)/|arxiv:\s*)", "", cleaned).removesuffix(".pdf")
    return cleaned


def _fts_query(value: str) -> str:
    tokens = [item.replace('"', '""') for item in re.findall(r"\w+", value, flags=re.UNICODE)]
    return " AND ".join(f'"{item}"' for item in tokens) or '""'


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS papers(
 paper_id TEXT PRIMARY KEY,title TEXT NOT NULL,normalized_title TEXT NOT NULL,abstract TEXT NOT NULL DEFAULT '',
 publication_year INTEGER,publication_date TEXT,language TEXT NOT NULL DEFAULT '',venue TEXT NOT NULL DEFAULT '',
 publisher TEXT NOT NULL DEFAULT '',license TEXT NOT NULL DEFAULT '',canonical_url TEXT,pdf_url TEXT,citation_key TEXT,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),metadata_json TEXT NOT NULL DEFAULT '{}',
 source_session_id TEXT,source_workspace TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(normalized_title,publication_year)
);
CREATE TABLE IF NOT EXISTS paper_identifiers(identifier_id TEXT PRIMARY KEY,paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 scheme TEXT NOT NULL,value TEXT NOT NULL,normalized_value TEXT NOT NULL,UNIQUE(scheme,normalized_value));
CREATE TABLE IF NOT EXISTS authors(author_id TEXT PRIMARY KEY,identity_key TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,
 given_name TEXT NOT NULL DEFAULT '',family_name TEXT NOT NULL DEFAULT '',orcid TEXT);
CREATE TABLE IF NOT EXISTS paper_authors(paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 author_id TEXT NOT NULL REFERENCES authors ON DELETE RESTRICT,position INTEGER NOT NULL,role TEXT NOT NULL,affiliation TEXT,
 PRIMARY KEY(paper_id,author_id));
CREATE TABLE IF NOT EXISTS paper_files(file_id TEXT PRIMARY KEY,paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 workspace_hash TEXT NOT NULL,workspace_root TEXT NOT NULL,relative_path TEXT NOT NULL,absolute_path TEXT NOT NULL,
 sha256 TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,is_primary INTEGER NOT NULL,
 source_session_id TEXT,added_at TEXT NOT NULL,
 UNIQUE(paper_id,sha256));
CREATE TABLE IF NOT EXISTS source_passages(passage_id TEXT PRIMARY KEY,paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 text TEXT NOT NULL,text_hash TEXT NOT NULL,context_before TEXT NOT NULL,context_after TEXT NOT NULL,page_start INTEGER,page_end INTEGER,
 section TEXT NOT NULL,paragraph TEXT NOT NULL,language TEXT NOT NULL,translation TEXT NOT NULL,extraction_method TEXT NOT NULL,
 verification_status TEXT NOT NULL,source_session_id TEXT,source_workspace TEXT,locator_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS uq_passage_location ON source_passages(paper_id,text_hash,COALESCE(page_start,0),section);
CREATE TABLE IF NOT EXISTS citation_examples(example_id TEXT PRIMARY KEY,paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 text TEXT NOT NULL,language TEXT NOT NULL,citation_style TEXT NOT NULL,claim TEXT NOT NULL,note TEXT NOT NULL,created_by TEXT NOT NULL,
 verification_status TEXT NOT NULL,source_session_id TEXT,source_workspace TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS citation_example_sources(example_id TEXT NOT NULL REFERENCES citation_examples ON DELETE CASCADE,
 paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,passage_id TEXT REFERENCES source_passages ON DELETE SET NULL,
 position INTEGER NOT NULL,relation_type TEXT NOT NULL,PRIMARY KEY(example_id,paper_id,passage_id));
CREATE TABLE IF NOT EXISTS tags(tag_id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS paper_tags(paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 tag_id TEXT NOT NULL REFERENCES tags ON DELETE CASCADE,PRIMARY KEY(paper_id,tag_id));
CREATE TABLE IF NOT EXISTS search_documents(rowid INTEGER PRIMARY KEY AUTOINCREMENT,document_id TEXT NOT NULL UNIQUE,
 entity_type TEXT NOT NULL CHECK(entity_type IN ('paper','passage','citation_example')),entity_id TEXT NOT NULL,
 paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,title TEXT NOT NULL,abstract TEXT NOT NULL,authors TEXT NOT NULL,
 keywords TEXT NOT NULL,body TEXT NOT NULL,search_text TEXT NOT NULL,language TEXT NOT NULL,publication_year INTEGER,
 content_hash TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS embeddings(document_id TEXT NOT NULL REFERENCES search_documents(document_id) ON DELETE CASCADE,
 model TEXT NOT NULL,dimensions INTEGER NOT NULL,vector BLOB NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,PRIMARY KEY(document_id,model));
CREATE TABLE IF NOT EXISTS embedding_jobs(job_id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES search_documents(document_id) ON DELETE CASCADE,
 model TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL,next_attempt_at TEXT,last_error TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(document_id,model));
CREATE INDEX IF NOT EXISTS ix_embedding_jobs_ready ON embedding_jobs(model,status,next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_search_documents_paper ON search_documents(paper_id,entity_type);
"""

_FTS_SCHEMA = """CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(
 title,abstract,authors,keywords,body,content='search_documents',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2'
)"""

_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS search_documents_ai AFTER INSERT ON search_documents BEGIN
 INSERT INTO search_documents_fts(rowid,title,abstract,authors,keywords,body)
 VALUES(new.rowid,new.title,new.abstract,new.authors,new.keywords,new.body);
END;
CREATE TRIGGER IF NOT EXISTS search_documents_ad AFTER DELETE ON search_documents BEGIN
 INSERT INTO search_documents_fts(search_documents_fts,rowid,title,abstract,authors,keywords,body)
 VALUES('delete',old.rowid,old.title,old.abstract,old.authors,old.keywords,old.body);
END;
CREATE TRIGGER IF NOT EXISTS search_documents_au AFTER UPDATE ON search_documents BEGIN
 INSERT INTO search_documents_fts(search_documents_fts,rowid,title,abstract,authors,keywords,body)
 VALUES('delete',old.rowid,old.title,old.abstract,old.authors,old.keywords,old.body);
 INSERT INTO search_documents_fts(rowid,title,abstract,authors,keywords,body)
 VALUES(new.rowid,new.title,new.abstract,new.authors,new.keywords,new.body);
END;
"""
