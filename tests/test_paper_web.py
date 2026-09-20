"""Workspace Read imports and disposable Agent PDF attachment tests."""

from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from bootstrap import ensure_project_initialized, ensure_workspace_initialized
from gateway.paper_web import PaperWebService
from reference import PaperUpsert, ReferenceStore


def pdf_bytes(*, title: str, width: float = 612) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=792)
    writer.add_metadata({"/Title": title})
    writer.write(output)
    return output.getvalue()


class PaperWebServiceTests(unittest.TestCase):
    def test_workspace_initialization_links_legacy_paper_files_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            agent_root, workspace = base / "agent", base / "workspace"
            agent_root.mkdir(); workspace.mkdir()
            ensure_project_initialized(agent_root)
            legacy_store = ReferenceStore(
                agent_root / ".yy" / "reference" / "reference.sqlite3",
            )
            paper = legacy_store.upsert_paper(PaperUpsert(title="Migrated Paper"))
            content = b"legacy-pdf"
            digest = hashlib.sha256(content).hexdigest()
            paper_root = agent_root / ".yy" / "papers" / "Migrated Paper"
            paper_root.mkdir(parents=True)
            (paper_root / "Migrated Paper.pdf").write_bytes(content)
            (paper_root / "Migrated Paper.md").write_text(
                "# Migrated summary\n", encoding="utf-8",
            )
            (agent_root / ".yy" / "papers" / "index.json").write_text(
                json.dumps({"version": 1, "papers": {digest: {
                    "paper_id": digest,
                    "title": "Migrated Paper",
                    "stem": "Migrated Paper",
                    "directory": "Migrated Paper",
                    "pdf_path": "Migrated Paper/Migrated Paper.pdf",
                    "summary_path": "Migrated Paper/Migrated Paper.md",
                    "status": "summarized",
                    "pdf_url": "https://example.test/paper.pdf",
                    "discovered_at": "2026-09-20T00:00:00+00:00",
                    "sha256": digest,
                    "content_type": "application/pdf",
                    "size_bytes": len(content),
                    "reference_paper_id": paper.paper_id,
                }}}, ensure_ascii=False),
                encoding="utf-8",
            )

            ensure_workspace_initialized(workspace, agent_root=agent_root)
            migrated = ReferenceStore(
                workspace / ".yy" / "reference" / "reference.sqlite3",
            )
            files = migrated.get_paper(paper.paper_id).files

            self.assertEqual(len(files), 1)
            self.assertEqual(Path(files[0].absolute_path).read_bytes(), content)
            self.assertEqual(len(migrated.list_papers(limit=201)), 1)
            summary = PaperWebService(
                workspace, migrated, attachments_root=base / "attachments",
            ).summary(paper.paper_id)
            self.assertEqual(summary["status"], "completed")
            self.assertIn("Migrated summary", summary["content"])

    def test_read_import_uses_pdf_title_and_never_overwrites_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "workspace"
            root.mkdir()
            store = ReferenceStore(root / ".yy" / "reference" / "reference.sqlite3")
            service = PaperWebService(
                root, store, attachments_root=Path(value) / "temporary-attachments",
            )

            first = service.import_pdf(
                pdf_bytes(title="Data-driven Optimization: Ship/Bunkering"),
                "2408.12345.pdf",
            )
            second = service.import_pdf(
                pdf_bytes(title="Data-driven Optimization: Ship/Bunkering", width=600),
                "another-name.pdf",
            )

            self.assertEqual(first["paper"].title, "Data-driven Optimization: Ship/Bunkering")
            self.assertEqual(first["filename"], "Data-driven Optimization Ship Bunkering.pdf")
            self.assertNotEqual(first["paper"].paper_id, second["paper"].paper_id)
            self.assertEqual(first["filename"], second["filename"])
            self.assertEqual(len(store.list_papers()), 2)
            for imported in (first, second):
                paper_root = root / ".yy" / "papers" / imported["paper"].paper_id
                self.assertEqual(len(tuple(paper_root.glob("*.pdf"))), 1)

            duplicate = service.import_pdf(
                pdf_bytes(title="Data-driven Optimization: Ship/Bunkering"),
                "third-name.pdf",
            )
            self.assertFalse(duplicate["created"])
            self.assertEqual(duplicate["paper"].paper_id, first["paper"].paper_id)

    def test_agent_attachment_is_temporary_and_does_not_create_a_paper(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "workspace"
            root.mkdir()
            attachment_root = Path(value) / "temporary-attachments"
            store = ReferenceStore(root / ".yy" / "reference" / "reference.sqlite3")
            service = PaperWebService(root, store, attachments_root=attachment_root)

            attachment = service.upload_attachment(
                pdf_bytes(title="Temporary Context"), "context.pdf",
            )
            resolved = service.resolve_attachments((attachment["attachment_id"],))

            self.assertEqual(store.list_papers(), ())
            self.assertFalse((root / ".yy" / "attachments").exists())
            self.assertTrue((attachment_root / f'{attachment["attachment_id"]}.pdf').is_file())
            self.assertEqual(resolved[0]["filename"], "context.pdf")
            self.assertNotIn("text", attachment)

    def test_summary_state_is_independent_from_pdf_reading(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "workspace"
            root.mkdir()
            store = ReferenceStore(root / ".yy" / "reference" / "reference.sqlite3")
            service = PaperWebService(
                root, store, attachments_root=Path(value) / "temporary-attachments",
            )
            imported = service.import_pdf(pdf_bytes(title="Summary Optional Paper"), "paper.pdf")
            paper_id = imported["paper"].paper_id

            self.assertEqual(service.summary(paper_id)["status"], "missing")
            service.mark_summary(paper_id, status="running", run_id="summary-run")
            self.assertEqual(service.summary(paper_id)["status"], "running")
            summary = service.write_summary(paper_id, "# Findings\n\nUseful result.", run_id="summary-run")
            self.assertEqual(summary["status"], "completed")
            self.assertIn("Useful result", summary["content"])

    def test_trash_retains_paper_then_purges_after_seven_days(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "workspace"
            root.mkdir()
            store = ReferenceStore(root / ".yy" / "reference" / "reference.sqlite3")
            service = PaperWebService(root, store, attachments_root=Path(value) / "attachments")
            imported = service.import_pdf(pdf_bytes(title="Trashable Paper"), "paper.pdf")
            paper_id = imported["paper"].paper_id
            directory = root / ".yy" / "papers" / paper_id

            trashed = service.trash_paper(paper_id)
            self.assertEqual(trashed.status, "archived")
            self.assertEqual(store.list_papers(), ())
            self.assertTrue(directory.is_dir())

            store.patch_paper_metadata(paper_id, {"purge_after": "2000-01-01T00:00:00+00:00"})
            self.assertEqual(service.purge_expired_papers(), 1)
            self.assertFalse(directory.exists())
            with self.assertRaises(KeyError):
                store.get_paper(paper_id)


if __name__ == "__main__":
    unittest.main()
