"""文件读写锁、Bash 工作区锁与跨进程互斥测试。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from sandbox import CommandResult, DockerSandboxSession, WorkspaceLockManager
from tools import ToolContext, default_tools


class _Checkpoint:
    commit_sha = "1" * 40


class _BlockingCheckpointSandbox:
    """把第一次 checkpoint 暂停，验证文件锁覆盖完整提交事务。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.contents: list[str] = []

    async def checkpoint_write(self, path: str):
        self.calls += 1
        self.contents.append((self.root / path).read_text(encoding="utf-8"))
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return _Checkpoint()

    async def restore_current(self):
        return None


class _BlockingDocker:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, arguments: list[str], timeout: float | None) -> CommandResult:
        del timeout
        if arguments[:2] == ["docker", "exec"]:
            self.entered.set()
            await self.release.wait()
        return CommandResult(returncode=0, stdout="ok")


class FileLockTests(unittest.TestCase):
    def test_lock_files_stay_in_agent_state_for_external_workspace(self) -> None:
        async def check(base: Path) -> None:
            agent_root = base / "agent"
            workspace = base / "workspace"
            agent_root.mkdir()
            workspace.mkdir()
            locks = WorkspaceLockManager(workspace, state_root=agent_root)

            async with locks.read(workspace / "note.txt"):
                pass

            self.assertFalse((workspace / ".yy").exists())
            self.assertEqual(
                len(list((agent_root / ".yy" / "sandbox" / "locks").glob("*/workspace.lock"))),
                1,
            )

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_same_file_allows_concurrent_readers(self) -> None:
        async def check(root: Path) -> None:
            locks = WorkspaceLockManager(root)
            path = root / "readers.txt"
            entered = asyncio.Event()

            async def second_reader() -> None:
                async with locks.read(path):
                    entered.set()

            async with locks.read(path):
                task = asyncio.create_task(second_reader())
                await asyncio.wait_for(entered.wait(), timeout=1)
            await task

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_read_waits_until_write_and_checkpoint_finish(self) -> None:
        async def check(root: Path) -> None:
            locks = WorkspaceLockManager(root)
            sandbox = _BlockingCheckpointSandbox(root)

            async def approve(name, arguments) -> bool:
                return True

            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=locks,
            )
            registry = default_tools(root)
            writer = asyncio.create_task(registry.execute(
                "write_file",
                {"path": "shared.txt", "content": "完整的新内容"},
                context,
            ))
            await asyncio.wait_for(sandbox.entered.wait(), timeout=2)
            reader = asyncio.create_task(registry.execute(
                "read_file",
                {"path": "shared.txt"},
                context,
            ))
            await asyncio.sleep(0.1)
            self.assertFalse(reader.done())
            sandbox.release.set()
            await asyncio.wait_for(writer, timeout=2)
            self.assertEqual(await asyncio.wait_for(reader, timeout=2), "完整的新内容")

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_two_writes_to_same_file_are_serialized_per_checkpoint(self) -> None:
        async def check(root: Path) -> None:
            locks = WorkspaceLockManager(root)
            sandbox = _BlockingCheckpointSandbox(root)

            async def approve(name, arguments) -> bool:
                return True

            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=locks,
            )
            registry = default_tools(root)
            first = asyncio.create_task(registry.execute(
                "write_file",
                {"path": "same.txt", "content": "第一版"},
                context,
            ))
            await asyncio.wait_for(sandbox.entered.wait(), timeout=2)
            second = asyncio.create_task(registry.execute(
                "write_file",
                {"path": "same.txt", "content": "第二版"},
                context,
            ))
            await asyncio.sleep(0.1)
            self.assertEqual(sandbox.calls, 1)
            sandbox.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
            self.assertEqual(sandbox.contents, ["第一版", "第二版"])
            self.assertEqual((root / "same.txt").read_text(encoding="utf-8"), "第二版")

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_unrelated_file_read_can_continue_during_write_checkpoint(self) -> None:
        async def check(root: Path) -> None:
            (root / "other.txt").write_text("可读取", encoding="utf-8")
            locks = WorkspaceLockManager(root)
            sandbox = _BlockingCheckpointSandbox(root)

            async def approve(name, arguments) -> bool:
                return True

            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=locks,
            )
            registry = default_tools(root)
            writer = asyncio.create_task(registry.execute(
                "write_file",
                {"path": "writing.txt", "content": "写入中"},
                context,
            ))
            await asyncio.wait_for(sandbox.entered.wait(), timeout=2)
            result = await asyncio.wait_for(
                registry.execute("read_file", {"path": "other.txt"}, context),
                timeout=1,
            )
            self.assertEqual(result, "可读取")
            sandbox.release.set()
            await writer

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_search_waits_for_locked_file(self) -> None:
        async def check(root: Path) -> None:
            locks = WorkspaceLockManager(root)
            sandbox = _BlockingCheckpointSandbox(root)

            async def approve(name, arguments) -> bool:
                return True

            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=locks,
            )
            registry = default_tools(root)
            writer = asyncio.create_task(registry.execute(
                "write_file",
                {"path": "search.txt", "content": "唯一检索内容"},
                context,
            ))
            await asyncio.wait_for(sandbox.entered.wait(), timeout=2)
            search = asyncio.create_task(registry.execute(
                "search_workspace",
                {"query": "唯一检索内容"},
                context,
            ))
            await asyncio.sleep(0.1)
            self.assertFalse(search.done())
            sandbox.release.set()
            await writer
            self.assertIn("search.txt", await asyncio.wait_for(search, timeout=2))

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_bash_holds_workspace_exclusive_lock(self) -> None:
        async def check(root: Path) -> None:
            (root / "visible.txt").write_text("内容", encoding="utf-8")
            docker = _BlockingDocker()
            sandbox = DockerSandboxSession(root, command_runner=docker)
            await sandbox.start("lock-bash")
            context = ToolContext(
                project_root=root,
                sandbox=sandbox,
                file_locks=sandbox.file_locks,
            )
            registry = default_tools(root)
            bash = asyncio.create_task(sandbox.run_bash("blocking-command"))
            await asyncio.wait_for(docker.entered.wait(), timeout=2)
            reader = asyncio.create_task(registry.execute(
                "read_file",
                {"path": "visible.txt"},
                context,
            ))
            await asyncio.sleep(0.1)
            self.assertFalse(reader.done())
            docker.release.set()
            await asyncio.wait_for(bash, timeout=3)
            self.assertEqual(await asyncio.wait_for(reader, timeout=2), "内容")
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_rollback_holds_workspace_exclusive_lock(self) -> None:
        async def check(root: Path) -> None:
            docker = _BlockingDocker()
            docker.release.set()
            sandbox = DockerSandboxSession(root, command_runner=docker)
            await sandbox.start("lock-rollback")
            (root / "value.txt").write_text("新版本", encoding="utf-8")
            await sandbox.checkpoint_write("value.txt")

            entered = threading.Event()
            release = threading.Event()
            original = sandbox.checkpoints.rollback

            def blocking_rollback(steps: int):
                entered.set()
                release.wait(timeout=5)
                return original(steps)

            sandbox.checkpoints.rollback = blocking_rollback
            rollback = asyncio.create_task(sandbox.rollback(1))
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
            context = ToolContext(
                project_root=root,
                sandbox=sandbox,
                file_locks=sandbox.file_locks,
            )
            reader = asyncio.create_task(
                default_tools(root).execute("read_file", {"path": "value.txt"}, context),
            )
            await asyncio.sleep(0.1)
            self.assertFalse(reader.done())
            release.set()
            await asyncio.wait_for(rollback, timeout=3)
            with self.assertRaises(FileNotFoundError):
                await asyncio.wait_for(reader, timeout=2)
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_cancelled_waiter_does_not_leave_lock(self) -> None:
        async def check(root: Path) -> None:
            locks = WorkspaceLockManager(root)
            path = root / "cancel.txt"
            async with locks.write(path):
                async def wait_to_read() -> None:
                    async with locks.read(path):
                        return None

                waiter = asyncio.create_task(wait_to_read())
                await asyncio.sleep(0.1)
                self.assertFalse(waiter.done())
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
            async def acquire_after_cancel() -> None:
                async with locks.read(path):
                    return None

            await asyncio.wait_for(acquire_after_cancel(), timeout=2)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_cross_process_workspace_lock_and_crash_release(self) -> None:
        async def check(root: Path) -> None:
            ready = root / "ready"
            release = root / "release"
            script = (
                "import asyncio,sys\n"
                "from pathlib import Path\n"
                "from sandbox import WorkspaceLockManager\n"
                "async def main():\n"
                " root,ready,release=map(Path,sys.argv[1:])\n"
                " locks=WorkspaceLockManager(root)\n"
                " async with locks.workspace_exclusive():\n"
                "  ready.write_text('ready')\n"
                "  while not release.exists(): await asyncio.sleep(0.05)\n"
                "asyncio.run(main())\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(root), str(ready), str(release)],
                cwd=Path(__file__).parents[1],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                await _wait_for_path(ready)
                locks = WorkspaceLockManager(root)

                async def acquire_shared() -> None:
                    async with locks.workspace_shared():
                        return None

                waiter = asyncio.create_task(acquire_shared())
                await asyncio.sleep(0.15)
                self.assertFalse(waiter.done())
                process.kill()
                await asyncio.to_thread(process.wait, 5)
                await asyncio.wait_for(waiter, timeout=3)
            finally:
                if process.poll() is None:
                    release.touch()
                    process.kill()
                    process.wait(timeout=5)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))


async def _wait_for_path(path: Path, timeout: float = 5) -> None:
    async def poll() -> None:
        while not path.exists():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout=timeout)
