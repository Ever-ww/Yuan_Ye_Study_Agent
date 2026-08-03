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

            second = await service.download_batch((candidate,), session_id="session-a")
            self.assertEqual(second.items[0].status, "duplicate")
            self.assertEqual((await service.get(paper_id)).status, "summarized")

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))
        self.assertEqual(requests, 1)

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
                "paper": {"title": "Library Link"},
                "batch_id": downloaded.batch_id,
                "library_paper_id": library_id,
            }, context)
            reference_id = json.loads(paper)["paper_id"]
            await registry.execute("reference_write", {
                "action": "link_file",
                "paper_id": reference_id,
                "scope": "paper_library",
                "batch_id": downloaded.batch_id,
                "library_paper_id": library_id,
            }, context)
            self.assertEqual(approvals, [])
            bundle = service.get("paper", reference_id)
            self.assertEqual(bundle["paper"]["files"][0]["workspace_hash"], "global-paper-library")
            self.assertEqual((await library.get(library_id)).reference_paper_id, reference_id)

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


if __name__ == "__main__":
    unittest.main()
