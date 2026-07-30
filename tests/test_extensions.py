from __future__ import annotations

import asyncio
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from Agent import (
    EventType,
    ExtensionContext,
    ExtensionLoader,
    HookEvent,
    HookPoint,
    HookRegistry,
    RunEvent,
    load_runtime_config,
)


def _write_extension(
    root: Path,
    stage: str,
    filename: str,
    name: str,
    priority: int,
    body: str = "context.state_root.mkdir(parents=True, exist_ok=True)",
) -> Path:
    target = root / "extension" / "hook" / stage / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(f"""\
        EXTENSION_NAME = {name!r}
        PRIORITY = {priority}

        async def handle(event, context):
            {body}
        """), encoding="utf-8")
    return target


class ExtensionLoaderTests(unittest.TestCase):
    def test_multiple_files_are_ordered_and_registered(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(
                root, "turn_start", "z_last.py", "last", 10,
                "event.data.setdefault('order', []).append('last')",
            )
            _write_extension(
                root, "turn_start", "b_second.py", "second", -10,
                "event.data.setdefault('order', []).append('second')",
            )
            _write_extension(
                root, "turn_start", "a_first.py", "first", -10,
                "event.data.setdefault('order', []).append('first')",
            )
            catalog = ExtensionLoader(root).scan()
            self.assertEqual(
                [item.name for item in catalog.modules[HookPoint.TURN_START]],
                ["first", "second", "last"],
            )
            registry = HookRegistry()
            catalog.register(registry, ExtensionContext(
                agent_root=root,
                source_root=root,
                workspace_root=root,
                state_root=root / ".yy" / "extension",
                provider="echo",
                model="echo",
                sandbox_enabled=False,
            ))
            event = HookEvent(point=HookPoint.TURN_START, session_id="session", data={})
            asyncio.run(registry.emit(event))
            self.assertEqual(event.data["order"], ["first", "second", "last"])

    def test_same_capability_can_span_stages(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(root, "trace_start", "metrics.py", "metrics", 0)
            _write_extension(root, "trace_end", "metrics.py", "metrics", 0)
            catalog = ExtensionLoader(root).scan()
            self.assertEqual(len(catalog.modules[HookPoint.TRACE_START]), 1)
            self.assertEqual(len(catalog.modules[HookPoint.TRACE_END]), 1)

    def test_duplicate_name_and_invalid_contract_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(root, "tool_after", "first.py", "duplicate", 0)
            _write_extension(root, "tool_after", "second.py", "duplicate", 1)
            with self.assertRaisesRegex(ValueError, "名称重复"):
                ExtensionLoader(root).scan()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_extension(root, "model_before", "broken.py", "broken", 51)
            with self.assertRaisesRegex(ValueError, "PRIORITY"):
                ExtensionLoader(root).scan()

    def test_repository_contains_all_ten_default_stage_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalog = ExtensionLoader(root).scan()
        for point in HookPoint:
            path = root / "extension" / "hook" / point.value / f"{point.value}.py"
            self.assertTrue(path.is_file(), path)
            self.assertTrue(catalog.modules[point], point)


class CodingSourceConfigTests(unittest.TestCase):
    def test_migration_marker_supplies_coding_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as home_folder, tempfile.TemporaryDirectory() as source_folder:
            home, source = Path(home_folder), Path(source_folder)
            marker = home / ".yy" / "agent-home-migration.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(
                '{"source_root": ' + repr(str(source)).replace("'", '"').replace("\\", "\\\\") + "}",
                encoding="utf-8",
            )
            config = load_runtime_config(home, workspace_root=source)
            self.assertEqual(config.coding_source_root, source.resolve())


class _FakeCodingRuntime:
    def __init__(self, config, worktree: Path) -> None:
        self.config = config.model_copy(update={"workspace_root": worktree})
        self.tool_context = SimpleNamespace(project_root=worktree)
        self.coding_session_id = "coding-memory"
        self.worktree = worktree
        self.closed = False

    async def run_task(self, task: str, session_id: str):
        del session_id
        import re
        match = re.search(r"`(tests/extensions/test_[a-z0-9_]+\.py)`", task)
        if match:
            extension = self.worktree / "extension" / "hook" / "turn_start" / "added_capability.py"
            extension.parent.mkdir(parents=True, exist_ok=True)
            extension.write_text(
                "EXTENSION_NAME = 'added-capability'\nPRIORITY = 0\n\n"
                "async def handle(event, context):\n    event.data['added'] = True\n",
                encoding="utf-8",
            )
            test = self.worktree / match.group(1)
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text("def test_added():\n    assert True\n", encoding="utf-8")
        yield RunEvent(type=EventType.FINAL, payload={"answer": "done"})

    async def close(self) -> None:
        self.closed = True


class CodeSessionControllerTests(unittest.TestCase):
    def test_worktree_is_under_agent_home_and_no_change_finalize_cleans(self) -> None:
        from run_ui.harness_loader import load_harness_module

        harness = load_harness_module()
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as home_folder:
            source, home = Path(source_folder), Path(home_folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", "seed"],
                cwd=source, check=True, capture_output=True,
            )
            config = load_runtime_config(
                home,
                workspace_root=source,
                coding_source_root=source,
                provider="echo",
                model="echo",
            )
            controller = harness.CodeSessionController(
                config,
                runtime_factory=lambda selected, worktree: _FakeCodingRuntime(selected, worktree),
            )

            async def scenario():
                record = await controller.start()
                expected = home / ".yy" / "harness-evolution" / "worktrees"
                self.assertIn(expected.resolve(), record.worktree_path.parents)
                result = await controller.finalize()
                self.assertEqual(result.status, "no_changes")
                self.assertFalse(record.worktree_path.exists())

            asyncio.run(scenario())

    def test_verified_turn_commits_and_finalize_fast_forwards(self) -> None:
        from run_ui.harness_loader import load_harness_module

        harness = load_harness_module()

        class FastController(harness.CodeSessionController):
            async def _validate_and_test(self, record, test_file):
                del record, test_file
                return {"passed": True, "feedback": ""}

        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as home_folder:
            source, home = Path(source_folder), Path(home_folder)
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", "seed"],
                cwd=source, check=True, capture_output=True,
            )
            config = load_runtime_config(
                home,
                workspace_root=source,
                coding_source_root=source,
                provider="echo",
                model="echo",
            )
            controller = FastController(
                config,
                runtime_factory=lambda selected, worktree: _FakeCodingRuntime(selected, worktree),
            )

            async def scenario():
                record = await controller.start()
                turn = await controller.run_turn("增加一项扩展能力")
                self.assertEqual(turn.status, "verified")
                self.assertNotEqual(turn.commit, record.base_commit)
                result = await controller.finalize()
                self.assertTrue(result.merged)
                self.assertTrue(
                    (source / "extension" / "hook" / "turn_start" / "added_capability.py").is_file()
                )

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
