"""全局论文库、Profile 读取、批次授权和内置 Skill 初始化测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import httpx
from pypdf import PdfWriter

from bootstrap import ensure_project_initialized
from paper_library import (
    PaperCandidate,
    PaperLibraryService,
    sanitize_paper_title,
    stable_paper_id,
)
from reference import ReferenceService, ReferenceStore
from skill import SkillService
from tool import AsyncToolRegistry, ToolContext
from tools import PaperDownloadTool
from tools.paper_library import PaperLibraryDownloadTool, PaperLibrarySaveTool
from tools.profile_read import ProfileReadTool
from tools.reference import ReferenceWriteTool


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("93.184.216.34",)


def _pdf_bytes() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


class PaperLibraryTests(unittest.TestCase):
    def test_profile_read_is_agent_home_scoped_and_rejects_symlink(self) -> None:
        async def run(root: Path) -> None:
            profile = root / ".yy" / "memory" / "profile"
            profile.mkdir(parents=True)
            (profile / "RESEARCH.md").write_text("graph learning", encoding="utf-8")
            tool = ProfileReadTool(root)
            context = ToolContext(project_root=root)
            self.assertEqual(
                await tool.run({"name": "RESEARCH"}, context),
                "graph learning",
            )
            with self.assertRaises(ValueError):
                await tool.run({"name": "../settings"}, context)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_stable_identity_and_cross_platform_title_sanitizing(self) -> None:
        first = PaperCandidate(
            title="A Study: Results?",
            pdf_url="https://example.com/a.pdf",
            doi="https://doi.org/10.1000/ABC",
        )
        second = PaperCandidate(
            title="Different title",
            pdf_url="https://mirror.example/b.pdf",
            doi="doi:10.1000/abc",
        )
        self.assertEqual(stable_paper_id(first), stable_paper_id(second))
        self.assertEqual(sanitize_paper_title(first.title), "A Study Results")
        self.assertEqual(sanitize_paper_title("CON"), "Paper - CON")

    def test_batch_download_deduplicates_and_save_uses_existing_grant(self) -> None:
        pdf = _pdf_bytes()
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)

        async def run(root: Path) -> None:
            downloader = PaperDownloadTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            service = PaperLibraryService(root, downloader=downloader)
            candidate = PaperCandidate(
                title="Attention Is All You Need",
                pdf_url="https://example.com/attention.pdf",
                arxiv_id="1706.03762",
                year=2017,
            )
            approvals: list[str] = []

            async def approve(name: str, arguments: dict) -> bool:
                del arguments
                approvals.append(name)
                return True

            registry = AsyncToolRegistry((
                PaperLibraryDownloadTool(service),
                PaperLibrarySaveTool(service),
            ))
            context = ToolContext(project_root=root, approval=approve, session_id="session-a")
            raw = await registry.execute(
                "paper_library_download",
                {"candidates": [candidate.model_dump(mode="json")]},
                context,
            )
            result = json.loads(raw)
            paper_id = result["items"][0]["paper_id"]
            batch_id = result["batch_id"]
            await registry.execute(
                "paper_library_save",
                {
                    "paper_id": paper_id,
                    "batch_id": batch_id,
                    "status": "summarized",
                    "markdown": "# Attention Is All You Need\n\n总结。",
                    "page_count": 1,
                    "pages_read": [1],
                },
                context,
            )
            self.assertEqual(approvals, ["paper_library_download"])
            record = await service.get(paper_id)
            self.assertEqual(record.status, "summarized")
            self.assertTrue((service.root / record.pdf_path).is_file())
            self.assertTrue((service.root / record.summary_path).is_file())

            restarted = PaperLibraryService(root, downloader=downloader)
            self.assertTrue(restarted.grant_allows(batch_id, paper_id, "session-a"))
            self.assertFalse(restarted.grant_allows(batch_id, paper_id, "different-session"))
            second = await restarted.download_batch((candidate,), session_id="session-a")
            self.assertEqual(second.items[0].status, "duplicate")
            self.assertEqual((await restarted.get(paper_id)).status, "summarized")

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))
        self.assertEqual(requests, 1)

    def test_legacy_successful_download_record_migrates_batch_grant(self) -> None:
        pdf = _pdf_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)

        async def run(root: Path) -> None:
            downloader = PaperDownloadTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            service = PaperLibraryService(root, downloader=downloader)
            candidate = PaperCandidate(
                title="Legacy Approved Paper",
                pdf_url="https://example.com/legacy.pdf",
                arxiv_id="2601.12345",
            )
            downloaded = await service.download_batch((candidate,), session_id="a" * 16)
            service.grants_path.unlink()
            session_dir = root / ".yy" / "memory" / "session" / ("b" * 16)
            session_dir.mkdir(parents=True)
            record = {
                "role": "tool",
                "name": "paper_library_download",
                "status": "success",
                "operation_id": "operation-1",
                "timestamp": "2026-08-09 13:27:37",
                "content": downloaded.model_dump_json(),
                "arguments": {"candidates": [candidate.model_dump(mode="json")]},
            }
            (session_dir / f"2026-08-09_{'a' * 16}_001.jsonl").write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            restarted = PaperLibraryService(root, downloader=downloader)
            paper_id = downloaded.items[0].paper_id
            self.assertTrue(restarted.grant_allows(downloaded.batch_id, paper_id, "a" * 16))
            persisted = json.loads(restarted.grants_path.read_text(encoding="utf-8"))
            self.assertIn(downloaded.batch_id, persisted["grants"])

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_reference_library_scope_requires_batch_and_links_global_pdf(self) -> None:
        pdf = _pdf_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)

        async def run(root: Path) -> None:
            library = PaperLibraryService(
                root,
                downloader=PaperDownloadTool(
                    transport=httpx.MockTransport(handler),
                    resolver=_public_resolver,
                ),
            )
            candidate = PaperCandidate(
                title="Library Link",
                pdf_url="https://example.com/library.pdf",
                doi="10.1000/library",
            )
            downloaded = await library.download_batch((candidate,), session_id="session-b")
            library_id = downloaded.items[0].paper_id
            # 模拟 Gateway/Runtime 重启：Reference 写入必须从 grants.json 恢复授权。
            library = PaperLibraryService(
                root,
                downloader=PaperDownloadTool(
                    transport=httpx.MockTransport(handler),
                    resolver=_public_resolver,
                ),
            )
            service = ReferenceService(ReferenceStore(root / ".yy" / "reference" / "reference.sqlite3"))
            tool = ReferenceWriteTool(service, library)
            registry = AsyncToolRegistry((tool,))
            approvals: list[str] = []

            async def approve(name: str, arguments: dict) -> bool:
                del arguments
                approvals.append(name)
                return True

            context = ToolContext(project_root=root, approval=approve, session_id="session-b")
            paper = await registry.execute("reference_write", {
                "action": "upsert_paper",
                "paper": {
                    "title": "Library Link",
                    "authors": ["Ada Lovelace"],
                    "year": 2026,
                    "doi": "10.1000/library",
                },
                "batch_id": downloaded.batch_id,
                "library_paper_id": library_id,
            }, context)
            reference_id = json.loads(paper)["paper_id"]
            passage = await registry.execute("reference_write", {
                "action": "add_passage",
                "passage": {
                    "text": "Verified source passage.",
                    "page": 3,
                    "locator": "p. 3, Results",
                },
                "scope": "paper_library",
                "batch_id": downloaded.batch_id,
                "library_paper_id": library_id,
            }, context)
            passage_id = json.loads(passage)["passage_id"]
            await registry.execute("reference_write", {
                "action": "link_file",
                "paper_id": reference_id,
                "scope": "paper_library",
                "batch_id": downloaded.batch_id,
                "library_paper_id": library_id,
            }, context)
            self.assertEqual(approvals, [])
            bundle = service.get("paper", reference_id)
            self.assertEqual(bundle["paper"]["authors"][0]["display_name"], "Ada Lovelace")
            self.assertEqual(bundle["paper"]["publication_year"], 2026)
            self.assertEqual(bundle["paper"]["identifiers"][0]["scheme"], "doi")
            self.assertEqual(bundle["paper"]["files"][0]["workspace_hash"], "global-paper-library")
            self.assertEqual(bundle["passages"][0]["paper_id"], reference_id)
            self.assertEqual(bundle["passages"][0]["page_start"], 3)
            self.assertEqual(bundle["passages"][0]["locator"]["label"], "p. 3, Results")
            self.assertEqual((await library.get(library_id)).reference_paper_id, reference_id)
            self.assertEqual((await library.get(library_id)).reference_passage_ids, (passage_id,))

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_initializer_registers_repository_skill_and_paper_index(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            result = ensure_project_initialized(root)
            self.assertTrue((result.yy_dir / "papers" / "index.json").is_file())
            repository_root = Path(__file__).resolve().parents[1]
            service = SkillService(root, root, repository_root)
            metadata = {item.name: item for item in service.catalog()}
            self.assertIn("search-summary-paper", metadata)
            self.assertIn("download", metadata["search-summary-paper"].description.casefold())
            skill_root = repository_root / "skills" / "search-summary-paper"
            instructions = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            template = (skill_root / "references" / "summary-template.md").read_text(
                encoding="utf-8",
            )
            self.assertIn("Reference as a citation-oriented evidence store", instructions)
            self.assertIn("2,000–4,000 Chinese characters", instructions)
            self.assertIn("一分钟概览", template)
            self.assertIn("方法与模型详解", template)
            self.assertIn("Reference 证据与引用索引", template)


if __name__ == "__main__":
    unittest.main()
