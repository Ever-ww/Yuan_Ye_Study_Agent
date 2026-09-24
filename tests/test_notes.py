from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from gateway.note_store import NoteConflict, NoteStore
from gateway.models import RunCreateRequest


class NoteStoreTests(unittest.TestCase):
    def test_notes_are_workspace_scoped_and_saved_as_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            first_root, second_root = Path(value) / "first", Path(value) / "second"
            first_root.mkdir(); second_root.mkdir()
            first, second = NoteStore(first_root), NoteStore(second_root)
            created = first.create(name="Route Plan", content="# Plan\n")

            self.assertEqual(len(first.list()), 1)
            self.assertEqual(second.list(), [])
            raw = (first_root / "notes" / "Route Plan.md").read_text(encoding="utf-8")
            self.assertIn("id: " + created["note_id"], raw)
            self.assertEqual(first.get(created["note_id"])["content"], "# Plan\n")

    def test_autosave_cas_and_folder_operations(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            store = NoteStore(root)
            folder = store.create(name="Research", kind="folder")
            note = store.create(name="Experiment", content="draft")
            moved = store.move(note["note_id"], folder["note_id"])
            saved = store.update(note["note_id"], content="result", expected_etag=moved["etag"])

            self.assertEqual(saved["parent_id"], folder["note_id"])
            self.assertEqual(saved["content"], "result")
            self.assertIn("Research", saved["path"])
            self.assertTrue((root / "notes" / "Research" / "Experiment.md").is_file())
            renamed = store.update(note["note_id"], name="Experiment Results", tags=["research", "result"])
            self.assertTrue((root / "notes" / "Research" / "Experiment Results.md").is_file())
            self.assertEqual(renamed["tags"], ["research", "result"])
            history = store.revisions(note["note_id"])
            self.assertGreaterEqual(len(history), 1)
            restored = store.restore_revision(note["note_id"], history[-1]["revision_id"])
            self.assertIn(restored["content"], {"draft", "result"})
            asset = store.save_asset(note["note_id"], "chart.png", b"png", kind="image")
            self.assertEqual(asset["path"], "assets/images/chart.png")
            self.assertEqual(store.asset_path(asset["path"]).read_bytes(), b"png")
            search_results = store.list("experiment")
            self.assertEqual(
                {item["note_id"] for item in search_results},
                {folder["note_id"], note["note_id"]},
            )
            with self.assertRaises(NoteConflict):
                store.update(note["note_id"], content="stale", expected_etag=moved["etag"])

            deleted = store.delete(folder["note_id"])
            self.assertEqual(store.list(), [])
            self.assertEqual(store.trash()[0]["trash_id"], deleted["trash_id"])
            restored_folder = store.restore_trash(deleted["trash_id"])
            self.assertEqual(restored_folder["kind"], "folder")
            self.assertEqual(
                {item["name"] for item in store.list()},
                {"Research", "Experiment Results"},
            )

    def test_frontmatter_reindex_and_wiki_backlinks(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            store = NoteStore(root)
            target = store.create(name="Gateway Architecture", content="# Gateway")
            source = store.create(name="Research Plan", content="See [[Gateway Architecture]].")
            links = store.get(source["note_id"])
            self.assertEqual(links["links"][0]["note_id"], target["note_id"])
            self.assertEqual(store.get(target["note_id"])["backlinks"][0]["note_id"], source["note_id"])
            renamed = store.update(target["note_id"], name="Gateway Internals")
            self.assertEqual(renamed["name"], "Gateway Internals")
            self.assertEqual(store.get(source["note_id"])["links"][0]["note_id"], target["note_id"])
            store.database_path.unlink()
            rebuilt = NoteStore(root)
            self.assertEqual({item["name"] for item in rebuilt.list()}, {"Gateway Internals", "Research Plan"})

    def test_reindex_recovers_physical_folders_and_plain_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            nested = root / "notes" / "Experiments"
            nested.mkdir(parents=True)
            (nested / "Trial.md").write_text("# Trial\n\nRaw Markdown", encoding="utf-8")
            store = NoteStore(root)
            rows = store.list()
            folder = next(item for item in rows if item["kind"] == "folder")
            note = next(item for item in rows if item["kind"] == "note")
            self.assertEqual(note["parent_id"], folder["note_id"])
            self.assertEqual(store.get(note["note_id"])["content"], "# Trial\n\nRaw Markdown")

    def test_external_markdown_changes_refresh_the_rebuildable_index(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            store = NoteStore(root)
            external = root / "notes" / "External.md"
            external.write_text("# External\n", encoding="utf-8")

            listed = store.list()
            note = next(item for item in listed if item["name"] == "External")
            self.assertEqual(store.get(note["note_id"])["content"], "# External\n")

            external.write_text("# Changed outside YYAgent\n", encoding="utf-8")
            self.assertEqual(
                store.get(note["note_id"])["content"],
                "# Changed outside YYAgent\n",
            )

    def test_file_watcher_refreshes_index_and_notifies_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            store = NoteStore(root)
            changed = threading.Event()
            observed: list[tuple[str, ...]] = []

            started = store.start_watcher(
                lambda paths: (observed.append(paths), changed.set()),
            )
            if not started:
                self.skipTest("platform file watcher is unavailable")
            try:
                external = root / "notes" / "Watched.md"
                external.write_text("# First\n", encoding="utf-8")
                self.assertTrue(changed.wait(5), "watcher did not report a file change")
                changed.clear()
                external.write_text("# Updated\n", encoding="utf-8")
                self.assertTrue(changed.wait(5), "watcher did not report the second change")
                note = next(item for item in store.list() if item["name"] == "Watched")
                self.assertEqual(store.get(note["note_id"])["content"], "# Updated\n")
                self.assertTrue(any("Watched.md" in path for batch in observed for path in batch))
            finally:
                store.stop_watcher()

    def test_plain_markdown_with_the_same_filename_keeps_both_notes(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "notes" / "A").mkdir(parents=True)
            (root / "notes" / "B").mkdir(parents=True)
            (root / "notes" / "A" / "Index.md").write_text("A", encoding="utf-8")
            (root / "notes" / "B" / "Index.md").write_text("B", encoding="utf-8")

            notes = [item for item in NoteStore(root).list() if item["kind"] == "note"]
            self.assertEqual(len(notes), 2)
            self.assertEqual(len({item["note_id"] for item in notes}), 2)

    def test_note_selection_is_a_valid_frozen_run_context(self) -> None:
        request = RunCreateRequest.model_validate({
            "project_id": "project",
            "client_id": "client",
            "task": "Explain this selection",
            "ui_context": {
                "source": "note",
                "resource": {
                    "kind": "workspace_file",
                    "logical_path": "YYWorkspace:\\notes\\Research.md",
                    "content_hash": "a" * 64,
                },
                "selection": {
                    "selected_text": "A stable paragraph",
                    "page": None,
                    "start_line": None,
                    "end_line": None,
                    "locator": {"note_id": "note-1"},
                },
            },
        }, strict=True)

        self.assertEqual(request.ui_context.source, "note")
        self.assertEqual(request.ui_context.selection.selected_text, "A stable paragraph")

    def test_markdown_import_reissues_identity_and_keeps_portable_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = NoteStore(Path(value))
            imported = store.create(
                name="Imported",
                content=(
                    "---\n"
                    "id: foreign-id\n"
                    "title: Foreign title\n"
                    "tags:\n"
                    "  - research\n"
                    "aliases:\n"
                    "  - Old title\n"
                    "---\n"
                    "# Body\n"
                ),
            )

            self.assertNotEqual(imported["note_id"], "foreign-id")
            self.assertEqual(imported["content"], "# Body\n")
            self.assertEqual(imported["tags"], ["research"])
            self.assertNotIn("foreign-id", store.raw_path(imported["note_id"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
