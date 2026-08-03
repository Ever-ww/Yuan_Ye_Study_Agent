"""公开论文 PDF 下载、写入事务和 Runtime 装配测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
from pypdf import PdfWriter

from Agent import AgentRuntime, load_runtime_config
from sandbox import WorkspaceLockManager
from tool import AsyncToolRegistry, ToolContext
from tools import (
    PaperDownloadResponse,
    PaperDownloadSecurityError,
    PaperDownloadServiceError,
    PaperDownloadTool,
)


async def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("93.184.216.34",)


def _pdf_bytes() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


class _CheckpointSandbox:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.restored = False

    async def checkpoint_write(self, path: str):
        self.paths.append(path)
        return SimpleNamespace(commit_sha="paper-checkpoint")

    async def restore_current(self):
        self.restored = True


class PaperDownloadTests(unittest.TestCase):
    def test_redirected_public_pdf_is_atomically_saved_and_checkpointed(self) -> None:
        pdf = _pdf_bytes()
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/paper":
                return httpx.Response(302, headers={"location": "/paper.pdf"})
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=pdf,
            )

        async def invoke(root: Path) -> PaperDownloadResponse:
            sandbox = _CheckpointSandbox()

            async def approve(name: str, arguments: dict) -> bool:
                self.assertEqual(name, "download_paper")
                self.assertEqual(arguments["path"], "papers/study.pdf")
                return True

            tool = PaperDownloadTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            registry = AsyncToolRegistry([tool])
            raw = await registry.execute(
                "download_paper",
                {"url": "https://example.com/paper", "path": "papers/study.pdf"},
                ToolContext(
                    project_root=root,
                    approval=approve,
                    sandbox=sandbox,
                    file_locks=WorkspaceLockManager(root),
                ),
            )
            result = PaperDownloadResponse.model_validate_json(raw, strict=True)
            self.assertEqual((root / result.path).read_bytes(), pdf)
            self.assertEqual(sandbox.paths, ["papers/study.pdf"])
            return result

        with tempfile.TemporaryDirectory() as value:
            result = asyncio.run(invoke(Path(value)))
        self.assertEqual(result.checkpoint, "paper-checkpoint")
        self.assertEqual(result.next_tool, "read_file")
        self.assertEqual(result.bytes_written, len(pdf))
        self.assertEqual(len(result.sha256), 64)
        self.assertEqual(len(requested), 2)

    def test_arxiv_abstract_url_is_normalized_to_official_pdf(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=_pdf_bytes(),
            )

        async def invoke(root: Path) -> None:
            tool = PaperDownloadTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            await tool.run(
                {
                    "url": "https://arxiv.org/abs/1706.03762",
                    "path": "papers/attention.pdf",
                },
                ToolContext(
                    project_root=root,
                    sandbox=_CheckpointSandbox(),
                    file_locks=WorkspaceLockManager(root),
                ),
            )

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(invoke(Path(value)))
        self.assertEqual(requested_paths, ["/pdf/1706.03762"])

    def test_existing_file_requires_explicit_overwrite_before_network_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_pdf_bytes())

        async def invoke(root: Path) -> None:
            (root / "paper.pdf").write_bytes(b"old")
            tool = PaperDownloadTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            with self.assertRaises(FileExistsError):
                await tool.run(
                    {"url": "https://example.com/paper.pdf", "path": "paper.pdf"},
                    ToolContext(
                        project_root=root,
                        sandbox=_CheckpointSandbox(),
                        file_locks=WorkspaceLockManager(root),
                    ),
                )
            self.assertEqual((root / "paper.pdf").read_bytes(), b"old")

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(invoke(Path(value)))
        self.assertEqual(calls, 0)

    def test_rejects_non_pdf_response_invalid_magic_and_unsafe_path(self) -> None:
        responses = iter((
            httpx.Response(200, headers={"content-type": "text/html"}, text="not pdf"),
            httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"not pdf"),
        ))

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return next(responses)

        async def invoke(root: Path) -> None:
            tool = PaperDownloadTool(
                transport=httpx.MockTransport(handler),
                resolver=_public_resolver,
            )
            context = ToolContext(
                project_root=root,
                sandbox=_CheckpointSandbox(),
                file_locks=WorkspaceLockManager(root),
            )
            with self.assertRaises(PaperDownloadServiceError):
                await tool.run(
                    {"url": "https://example.com/page", "path": "page.pdf"},
                    context,
                )
            with self.assertRaises(PaperDownloadSecurityError):
                await tool.run(
                    {"url": "https://example.com/fake.pdf", "path": "fake.pdf"},
                    context,
                )
            with self.assertRaises(PermissionError):
                await tool.run(
                    {"url": "https://example.com/paper.pdf", "path": "../paper.pdf"},
                    context,
                )
            with self.assertRaises(PaperDownloadSecurityError):
                await tool.run(
                    {"url": "https://example.com/paper", "path": "paper.bin"},
                    context,
                )

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(invoke(Path(value)))

    def test_runtime_exposes_download_tool_with_configured_limits(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            runtime = AgentRuntime(
                load_runtime_config(root, workspace_root=root),
                enable_sandbox=False,
                enable_extensions=False,
            )
            self.assertIn("download_paper", runtime.tools.names())
            tool = runtime.tools._tools["download_paper"]
            self.assertEqual(tool.max_bytes, 50_000_000)
            self.assertEqual(tool.timeout_seconds, 60)
            subagent_schema = next(
                item for item in runtime.tools.schemas() if item["name"] == "subagent"
            )
            delegated = subagent_schema["parameters"]["properties"]["tools"]["items"]["enum"]
            self.assertIn("download_paper", delegated)
            self.assertEqual(
                runtime.tools.risk_of(
                    "subagent",
                    {"task": "下载论文", "tools": ["download_paper"]},
                ),
                "write",
            )


if __name__ == "__main__":
    unittest.main()
