"""全局 Reference 数据库、混合检索、向量队列与工具权限测试。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx

from Agent import AgentRuntime, load_runtime_config
from bootstrap import ensure_project_initialized, is_project_initialized
from reference import (
    Author,
    CitationExampleCreate,
    OpenAIEmbeddingProvider,
    PaperFile,
    PaperIdentifier,
    PaperUpsert,
    ReferenceSearchRequest,
    ReferenceEmbeddingWorker,
    ReferenceService,
    ReferenceStore,
    SourcePassageCreate,
    pack_vector,
    unpack_vector,
)
from tool import AsyncToolRegistry, ToolContext
from tools.reference import ReferenceGetTool, ReferenceSearchTool, ReferenceWriteTool


class FakeEmbeddingProvider:
    model = "test-embedding"

    async def embed(self, texts):
        return tuple((1.0, 0.0) if "transformer" in text.casefold() else (0.0, 1.0) for text in texts)


class ReferenceTests(unittest.TestCase):
    def test_v1_papers_schema_is_backed_up_and_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            database = Path(value) / "reference.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE papers(
                        paper_id TEXT PRIMARY KEY,title TEXT NOT NULL,
                        normalized_title TEXT NOT NULL,abstract TEXT NOT NULL DEFAULT '',
                        publication_year INTEGER,publication_date TEXT,
                        language TEXT NOT NULL DEFAULT '',venue TEXT NOT NULL DEFAULT '',
                        publisher TEXT NOT NULL DEFAULT '',license TEXT NOT NULL DEFAULT '',
                        canonical_url TEXT,pdf_url TEXT,citation_key TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                    );
                    PRAGMA user_version=1;
                """)
                connection.execute(
                    "INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "legacy", "Legacy Paper", "legacy paper", "", 2020, None,
                        "", "", "", "", None, None, None, "active", "{}",
                        "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00",
                    ),
                )

            store = ReferenceStore(database)
            connection = sqlite3.connect(database)
            try:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(papers)").fetchall()
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
            self.assertIn("source_session_id", columns)
            self.assertIn("source_workspace", columns)
            self.assertEqual(version, 2)
            self.assertIsNotNone(store.migration_backup_path)
            self.assertTrue(store.migration_backup_path.is_file())
            self.assertEqual(store.get_paper("legacy").title, "Legacy Paper")
            created = store.upsert_paper(PaperUpsert(
                title="New Paper",
                source_session_id="session",
                source_workspace="D:/workspace",
            ))
            self.assertEqual(created.source_session_id, "session")

    def test_initializer_creates_global_reference_database_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            result = ensure_project_initialized(root)
            database = result.yy_dir / "reference" / "reference.sqlite3"
            self.assertTrue(database.is_file())
            self.assertTrue(is_project_initialized(root))
            store = ReferenceStore(database)
            paper = store.upsert_paper(PaperUpsert(title="Preserved", publication_year=2024))
            ensure_project_initialized(root)
            self.assertEqual(ReferenceStore(database).get_paper(paper.paper_id).title, "Preserved")
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            finally:
                connection.close()

    def test_schema_normalizes_papers_passages_examples_and_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = ReferenceStore(Path(value) / "reference.sqlite3")
            service = ReferenceService(store)
            paper = service.upsert_paper(PaperUpsert(
                title="Attention Is All You Need", abstract="Transformer architecture",
                publication_year=2017, language="en",
                identifiers=(PaperIdentifier(scheme="doi", value="https://doi.org/10.0000/demo"),),
                authors=(Author(display_name="Ashish Vaswani", affiliation="Google"),),
                tags=("Transformer", "AI"), metadata={"source": "test"},
            ))
            duplicate = service.upsert_paper(PaperUpsert(
                title="Different supplied title", publication_year=2017,
                identifiers=(PaperIdentifier(scheme="doi", value="10.0000/demo"),),
            ))
            self.assertEqual(duplicate.paper_id, paper.paper_id)
            passage = service.add_passage(SourcePassageCreate(
                paper_id=paper.paper_id, text="We propose a new simple network architecture.",
                page_start=1, section="Abstract", verification_status="verified",
                source_workspace="D:/research",
            ))
            example = service.add_citation_example(CitationExampleCreate(
                paper_id=paper.paper_id, text="Vaswani 等提出了 Transformer 架构。",
                source_passage_ids=(passage.passage_id,), citation_style="GB/T 7714",
            ))
            file = PaperFile(
                workspace_hash="a" * 16, workspace_root="D:/research", relative_path="papers/a.pdf",
                absolute_path="D:/research/papers/a.pdf", sha256="b" * 64,
                size_bytes=1234,
            )
            service.add_file(paper.paper_id, file)
            bundle = service.get("paper", paper.paper_id)
            self.assertEqual(len(bundle["passages"]), 1)
            self.assertEqual(bundle["citation_examples"][0]["source_passage_ids"], [passage.passage_id])
            self.assertEqual(bundle["paper"]["files"][0]["sha256"], "b" * 64)
            self.assertNotIn("%PDF", json.dumps(bundle, ensure_ascii=False))
            archived = service.archive(paper.paper_id)
            self.assertEqual(archived.status, "archived")
            self.assertEqual(service.restore(paper.paper_id).status, "active")
            self.assertEqual(example.paper_id, paper.paper_id)

    def test_fts_and_all_three_hybrid_modes(self) -> None:
        async def run(directory: Path):
            provider = FakeEmbeddingProvider()
            store = ReferenceStore(directory / "reference.sqlite3")
            service = ReferenceService(store, provider)
            first = service.upsert_paper(PaperUpsert(
                title="Transformer Study", abstract="attention transformer", publication_year=2024,
            ))
            second = service.upsert_paper(PaperUpsert(
                title="Graph Study", abstract="graph neural network", publication_year=2023,
            ))
            for document_id, vector in (
                (f"paper:{first.paper_id}", (1.0, 0.0)),
                (f"paper:{second.paper_id}", (0.0, 1.0)),
            ):
                store.queue_embedding(document_id, provider.model)
                job = store.claim_embedding_job(provider.model)
                assert job is not None
                store.complete_embedding(job, pack_vector(vector), 2)
            results = {}
            for mode in ("rrf", "weighted", "separate"):
                results[mode] = await service.search(ReferenceSearchRequest(query="transformer", mode=mode))
            return store, results

        with tempfile.TemporaryDirectory() as value:
            store, results = asyncio.run(run(Path(value)))
            self.assertTrue(store.fts_available)
            self.assertEqual(results["rrf"].results[0].title, "Transformer Study")
            self.assertEqual(results["weighted"].results[0].title, "Transformer Study")
            self.assertTrue(results["separate"].lexical_results)
            self.assertTrue(results["separate"].semantic_results)
            self.assertEqual(unpack_vector(pack_vector((0.25, 0.75)), 2), (0.25, 0.75))

    def test_embedding_http_contract_does_not_expose_api_key(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

        async def run():
            provider = OpenAIEmbeddingProvider(
                "https://embedding.example/v1", "secret-key", "embed-model",
                transport=httpx.MockTransport(handler),
            )
            return await provider.embed(("paper text",))

        vectors = asyncio.run(run())
        self.assertEqual(len(vectors[0]), 2)
        self.assertEqual(requests[0].url.path, "/v1/embeddings")
        self.assertEqual(requests[0].headers["authorization"], "Bearer secret-key")

    def test_embedding_worker_persists_vectors_and_recovers_running_jobs(self) -> None:
        async def run(root: Path):
            store = ReferenceStore(root / "reference.sqlite3")
            provider = FakeEmbeddingProvider()
            worker = ReferenceEmbeddingWorker(store, provider)
            service = ReferenceService(store, provider, worker=worker)
            await worker.start()
            try:
                paper = service.upsert_paper(PaperUpsert(title="Transformer worker"))
                request = ReferenceSearchRequest(query="transformer")
                for _ in range(100):
                    if store.documents_for_embeddings(request):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(store.documents_for_embeddings(request))
            finally:
                await worker.close()

            second = service.upsert_paper(PaperUpsert(title="Pending recovery"))
            document_id = f"paper:{second.paper_id}"
            store.queue_embedding(document_id, provider.model)
            claimed = store.claim_embedding_job(provider.model)
            self.assertIsNotNone(claimed)
            reopened = ReferenceStore(root / "reference.sqlite3")
            recovered = reopened.claim_embedding_job(provider.model)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.document_id, document_id)
            for attempts in range(5):
                reopened.fail_embedding(recovered.model_copy(update={"attempts": attempts}), "secret-free error")
            connection = sqlite3.connect(reopened.database_path)
            try:
                row = connection.execute(
                    "SELECT status,attempts,last_error FROM embedding_jobs WHERE job_id=?",
                    (recovered.job_id,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("failed", 5, "secret-free error"))

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_reference_tools_expose_read_without_approval_and_guard_writes(self) -> None:
        async def run(root: Path):
            service = ReferenceService(ReferenceStore(root / ".yy" / "reference" / "reference.sqlite3"))
            registry = AsyncToolRegistry((
                ReferenceSearchTool(service), ReferenceGetTool(service), ReferenceWriteTool(service),
            ))
            approvals = []

            async def approve(name, arguments):
                approvals.append((name, arguments))
                return True

            context = ToolContext(project_root=root, approval=approve)
            paper_json = await registry.execute("reference_write", {
                "action": "upsert_paper",
                "paper": {"title": "Tool Paper", "publication_year": 2025},
            }, context)
            paper_id = json.loads(paper_json)["paper_id"]
            search = await registry.execute("reference_search", {"query": "Tool"}, context)
            return registry, approvals, paper_id, search

        with tempfile.TemporaryDirectory() as value:
            registry, approvals, paper_id, search = asyncio.run(run(Path(value)))
            self.assertEqual([item[0] for item in approvals], ["reference_write"])
            self.assertIn(paper_id, search)
            self.assertEqual(registry.risk_of("reference_search"), "read")
            self.assertEqual(registry.risk_of("reference_write"), "write")

    def test_runtime_config_and_default_tools_use_global_reference(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            config = load_runtime_config(root, reference_search_mode="separate")
            runtime = AgentRuntime(config, enable_sandbox=False, enable_subagent=False)
            try:
                self.assertEqual(config.reference_database_path, root / ".yy" / "reference" / "reference.sqlite3")
                self.assertIn("reference_search", runtime.tools.names(runtime.tool_context))
                self.assertIn("reference_get", runtime.tools.names(runtime.tool_context))
                self.assertIn("reference_write", runtime.tools.names(runtime.tool_context))
            finally:
                asyncio.run(runtime.close())

    def test_shared_config_rejects_reference_embedding_key(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            ensure_project_initialized(root)
            (root / ".yy" / "settings.json").write_text(
                '{"reference_embedding_api_key":"must-stay-local"}', encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "settings.local.json"):
                load_runtime_config(root)


if __name__ == "__main__":
    unittest.main()
