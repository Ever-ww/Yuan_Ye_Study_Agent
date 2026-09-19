from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gateway.workspace_files import WorkspaceFileConflict, WorkspaceFileService


class WorkspaceFileServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_logical_tree_read_write_and_cas(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value).resolve()
            source = root / "agent-source"
            workspace = root / "workspace"
            source.mkdir(); workspace.mkdir()
            (workspace / "note.md").write_text("old", encoding="utf-8")
            service = WorkspaceFileService(root, source)

            tree = await service.tree(workspace)
            self.assertEqual(tree["entries"][0]["path"], "YYWorkspace:\\note.md")
            opened = await service.read(workspace, "YYWorkspace:\\note.md")
            saved = await service.write(
                workspace, "YYWorkspace:\\note.md", "new",
                expected_etag=str(opened["etag"]),
            )
            self.assertEqual((workspace / "note.md").read_text(encoding="utf-8"), "new")
            with self.assertRaises(WorkspaceFileConflict):
                await service.write(
                    workspace, "YYWorkspace:\\note.md", "stale",
                    expected_etag=str(opened["etag"]),
                )
            self.assertEqual(len(str(saved["etag"])), 64)

    async def test_path_escape_and_hardlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value).resolve()
            source = root / "agent-source"
            workspace = root / "workspace"
            source.mkdir(); workspace.mkdir()
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            service = WorkspaceFileService(root, source)
            with self.assertRaises(PermissionError):
                await service.read(workspace, "YYWorkspace:\\..\\outside.txt")
            linked = workspace / "linked.txt"
            try:
                os.link(outside, linked)
            except OSError:
                self.skipTest("hard links are unavailable")
            with self.assertRaises(PermissionError):
                await service.read(workspace, "YYWorkspace:\\linked.txt")

    async def test_delete_returns_recoverable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value).resolve()
            source = root / "agent-source"
            workspace = root / "workspace"
            source.mkdir(); workspace.mkdir()
            target = workspace / "draft.md"
            target.write_text("recover me", encoding="utf-8")
            service = WorkspaceFileService(root, source)
            result = await service.delete(workspace, "YYWorkspace:\\draft.md")
            self.assertFalse(target.exists())
            self.assertTrue(result["checkpoint_id"])
            self.assertTrue(result["result_checkpoint_id"])


if __name__ == "__main__":
    unittest.main()
