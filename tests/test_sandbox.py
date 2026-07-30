"""Docker 生命周期与独立本地 checkpoint 的确定性回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from Agent import AgentRuntime, HookPoint, HookRegistry, load_runtime_config
from Agent.contracts import ModelReply
from sandbox import CheckpointStore, CommandResult, DockerSandboxSession
from tools import ToolContext, default_tools


class _AnswerProvider:
    streaming = False

    async def complete(self, messages, tools):
        return ModelReply(text="完成")


class _FakeDocker:
    """只模拟 Docker 控制面；checkpoint 仍使用真实本地 Git。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []

    async def __call__(self, arguments: list[str], timeout: float | None) -> CommandResult:
        del timeout
        self.calls.append(list(arguments))
        if arguments[:2] == ["docker", "exec"]:
            command = arguments[-1]
            if command == "write-many":
                (self.root / "first.txt").write_text("一", encoding="utf-8")
                (self.root / "second.txt").write_text("二", encoding="utf-8")
                return CommandResult(returncode=0, stdout="written")
            if command == "write-then-fail":
                (self.root / "failed.txt").write_text("不应保留", encoding="utf-8")
                return CommandResult(returncode=9, stderr="failed")
            return CommandResult(returncode=0, stdout="read only")
        return CommandResult(returncode=0, stdout="ok")


class SandboxTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("YY_RUN_DOCKER_TESTS") == "1" and shutil.which("docker"),
        "设置 YY_RUN_DOCKER_TESTS=1 且安装 Docker 后运行真实容器集成测试",
    )
    def test_real_docker_integration_when_explicitly_enabled(self) -> None:
        async def check(root: Path) -> None:
            sandbox = DockerSandboxSession(root)
            await sandbox.start("real-docker")
            result = await sandbox.run_bash("printf 'docker-ok'")
            self.assertIn("docker-ok", result.output)
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_bash_and_rollback_require_unified_approval(self) -> None:
        async def check(root: Path) -> None:
            registry = default_tools(root)
            context = ToolContext(project_root=root, sandbox=object())
            with self.assertRaises(PermissionError):
                await registry.execute("bash", {"command": "pwd"}, context)
            with self.assertRaises(PermissionError):
                await registry.execute("sandbox_rollback", {"steps": 1}, context)

            async def approve(name, arguments) -> bool:
                return True

            without_checkpoint = ToolContext(project_root=root, approval=approve)
            with self.assertRaisesRegex(RuntimeError, "未启用 checkpoint"):
                await registry.execute(
                    "write",
                    {"path": "must-not-exist.txt", "content": "x"},
                    without_checkpoint,
                )
            self.assertFalse((root / "must-not-exist.txt").exists())

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_docker_failure_stops_trace_without_host_fallback(self) -> None:
        async def unavailable(arguments: list[str], timeout: float | None) -> CommandResult:
            del timeout
            if arguments[:2] == ["docker", "version"]:
                return CommandResult(returncode=1, stderr="daemon unavailable")
            self.fail(f"Docker 检查失败后不应继续执行：{arguments}")

        async def check(root: Path) -> None:
            sandbox = DockerSandboxSession(root, command_runner=unavailable)
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=_AnswerProvider(),
                sandbox=sandbox,
                raise_errors=True,
            )
            with self.assertRaisesRegex(RuntimeError, "Docker 服务不可用"):
                await runtime.run("不能降级")
            self.assertFalse(sandbox.active)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_later_trace_start_failure_still_removes_started_container(self) -> None:
        async def check(root: Path) -> list[list[str]]:
            fake = _FakeDocker(root)
            sandbox = DockerSandboxSession(root, command_runner=fake)
            hooks = HookRegistry()

            async def fail_after_sandbox(event) -> None:
                raise RuntimeError("后续初始化失败")

            hooks.register(HookPoint.TRACE_START, fail_after_sandbox, priority=-200)
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=_AnswerProvider(),
                hooks=hooks,
                sandbox=sandbox,
                raise_errors=True,
            )
            with self.assertRaisesRegex(RuntimeError, "后续初始化失败"):
                await runtime.run("触发失败")
            self.assertFalse(sandbox.active)
            return fake.calls

        with tempfile.TemporaryDirectory() as value:
            calls = asyncio.run(check(Path(value)))
            self.assertEqual(sum(call[:2] == ["docker", "run"] for call in calls), 1)
            self.assertEqual(sum(call[:3] == ["docker", "rm", "--force"] for call in calls), 1)

    def test_runtime_starts_and_closes_injected_sandbox_once(self) -> None:
        async def check(root: Path) -> tuple[_FakeDocker, DockerSandboxSession]:
            fake = _FakeDocker(root)
            sandbox = DockerSandboxSession(root, command_runner=fake)
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=_AnswerProvider(),
                sandbox=sandbox,
            )
            result = await runtime.run("测试")
            self.assertTrue(result.completed)
            return fake, sandbox

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            fake, sandbox = asyncio.run(check(root))
            self.assertFalse(sandbox.active)
            self.assertEqual(sum(call[:2] == ["docker", "run"] for call in fake.calls), 1)
            self.assertEqual(sum(call[:3] == ["docker", "rm", "--force"] for call in fake.calls), 1)

    def test_docker_run_uses_required_isolation_and_masks_sensitive_paths(self) -> None:
        async def check(root: Path) -> list[list[str]]:
            for relative in (".git", ".yy", ".venv"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            (root / ".env.local").write_text("SECRET=x", encoding="utf-8")
            fake = _FakeDocker(root)
            sandbox = DockerSandboxSession(root, command_runner=fake)
            await sandbox.start("session")
            await sandbox.close()
            return fake.calls

        with tempfile.TemporaryDirectory() as value:
            calls = asyncio.run(check(Path(value)))
            run = next(call for call in calls if call[:2] == ["docker", "run"])
            joined = "\n".join(run)
            self.assertIn("none", run)
            self.assertIn("ALL", run)
            self.assertIn("no-new-privileges:true", run)
            self.assertIn("--read-only", run)
            self.assertIn("target=/workspace", joined)
            self.assertIn("/workspace/.git", joined)
            self.assertIn("/workspace/.yy", joined)
            self.assertIn("/workspace/.env.local", joined)

    def test_bash_checkpoints_once_and_failed_command_restores(self) -> None:
        async def check(root: Path) -> None:
            fake = _FakeDocker(root)
            sandbox = DockerSandboxSession(root, checkpoint_limit=17, command_runner=fake)
            await sandbox.start("bash-session")
            baseline_count = len(sandbox.list_checkpoints())
            written = await sandbox.run_bash("write-many")
            self.assertEqual(written.output, "written")
            self.assertIsNotNone(written.checkpoint)
            self.assertEqual(len(sandbox.list_checkpoints()), baseline_count + 1)
            unchanged = await sandbox.run_bash("read-only")
            self.assertIsNone(unchanged.checkpoint)
            self.assertEqual(len(sandbox.list_checkpoints()), baseline_count + 1)
            with self.assertRaisesRegex(RuntimeError, "exit=9"):
                await sandbox.run_bash("write-then-fail")
            self.assertFalse((root / "failed.txt").exists())
            self.assertTrue((root / "first.txt").exists())
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_write_creates_checkpoint_and_rollback_restores_workspace(self) -> None:
        async def check(root: Path) -> None:
            fake = _FakeDocker(root)
            sandbox = DockerSandboxSession(root, command_runner=fake)
            await sandbox.start("write-session")

            async def approve(name, arguments) -> bool:
                return True

            registry = default_tools(root)
            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=sandbox.file_locks,
            )
            first = await registry.execute(
                "write",
                {"path": "note.txt", "content": "第一版"},
                context,
            )
            self.assertIn("checkpoint", first)
            count = len(sandbox.list_checkpoints())
            unchanged = await registry.execute(
                "write",
                {"path": "note.txt", "content": "第一版"},
                context,
            )
            self.assertIn("未创建 checkpoint", unchanged)
            self.assertEqual(len(sandbox.list_checkpoints()), count)
            await registry.execute(
                "write",
                {"path": "note.txt", "content": "第二版"},
                context,
            )
            result = await registry.execute("sandbox_rollback", {"steps": 1}, context)
            self.assertIn("已恢复 checkpoint", result)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "第一版")
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_edit_creates_an_edit_checkpoint(self) -> None:
        async def check(root: Path) -> None:
            fake = _FakeDocker(root)
            sandbox = DockerSandboxSession(root, command_runner=fake)
            await sandbox.start("edit-session")
            (root / "note.txt").write_text("before", encoding="utf-8")
            sandbox.checkpoints.create("fixture", force=True)

            async def approve(name, arguments) -> bool:
                del name, arguments
                return True

            registry = default_tools(root)
            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=sandbox.file_locks,
            )
            result = await registry.execute(
                "edit",
                {
                    "path": "note.txt",
                    "edits": [{"oldText": "before", "newText": "after"}],
                },
                context,
            )
            latest = sandbox.list_checkpoints()[-1]
            self.assertEqual(latest.source, "edit")
            self.assertEqual(latest.metadata, {"path": "note.txt"})
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "after")
            self.assertIn(latest.commit_sha, result)
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_limit_physically_prunes_old_root_commit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".gitignore").write_text(".yy/\n", encoding="utf-8")
            (root / "value.txt").write_text("0", encoding="utf-8")
            store = CheckpointStore(root, limit=3)
            store.open("limit-session")
            oldest = store.create("trace_start", force=True)
            self.assertIsNotNone(oldest)
            for number in range(1, 5):
                (root / "value.txt").write_text(str(number), encoding="utf-8")
                store.create("write", {"number": number})
            self.assertEqual(len(store.list()), 3)
            git_dir = root / ".yy" / "sandbox" / "checkpoints" / "limit-session" / "repository.git"
            missing = subprocess.run(
                ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{oldest.commit_sha}^{{commit}}"],
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)
            state = json.loads(
                (root / ".yy" / "sandbox" / "checkpoints" / "limit-session" / "index.json").read_text(
                    encoding="utf-8",
                ),
            )
            self.assertEqual(len(state["checkpoints"]), 3)

    def test_checkpoint_captures_workspace_but_stores_objects_in_agent_state(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            agent_root = base / "agent"
            workspace = base / "workspace"
            agent_root.mkdir()
            workspace.mkdir()
            target = workspace / "note.txt"
            target.write_text("初始内容", encoding="utf-8")

            store = CheckpointStore(workspace, state_root=agent_root)
            store.open("external-workspace")
            store.create("trace_start", force=True)
            target.write_text("修改内容", encoding="utf-8")
            store.create("write")
            target.write_text("未提交内容", encoding="utf-8")
            store.restore_current()

            self.assertEqual(target.read_text(encoding="utf-8"), "修改内容")
            self.assertFalse((workspace / ".yy").exists())
            checkpoint_roots = list(
                (agent_root / ".yy" / "sandbox" / "checkpoints").glob(
                    "*/external-workspace/repository.git",
                ),
            )
            self.assertEqual(len(checkpoint_roots), 1)

    def test_checkpoint_store_never_changes_project_git_head(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            (root / ".gitignore").write_text(".yy/\n", encoding="utf-8")
            (root / "tracked.txt").write_text("before", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "baseline"], check=True, capture_output=True)
            before = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            store = CheckpointStore(root)
            store.open("head-session")
            store.create("trace_start", force=True)
            (root / "tracked.txt").write_text("after", encoding="utf-8")
            store.create("write")
            after = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(before, after)
