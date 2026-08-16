"""Docker 生命周期与独立本地 checkpoint 的确定性回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from Agent import AgentRuntime, EventType, HookPoint, HookRegistry, load_runtime_config
from Agent.contracts import ModelReply, ToolCall
from sandbox import (
    BashUnavailableError,
    CheckpointDreamCoordinator,
    CheckpointBranchRecord,
    CheckpointStore,
    CheckpointValueAssessment,
    CommandResult,
    DockerSandboxSession,
)
from tool import ToolContext, default_tools, register_subagent


class _AnswerProvider:
    streaming = False

    async def complete(self, messages, tools):
        return ModelReply(text="完成")


class _CapturingProvider(_AnswerProvider):
    def __init__(self) -> None:
        self.tool_names: list[set[str]] = []
        self.system_prompts: list[str] = []
        self.user_queries: list[str] = []

    async def complete(self, messages, tools):
        self.tool_names.append({str(item["name"]) for item in tools})
        self.system_prompts.append(str(messages[0]["content"]))
        self.user_queries.append(str(messages[-1]["content"]))
        return await super().complete(messages, tools)


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
    def test_branch_lifecycle_and_merge_state_are_separate_invariants(self) -> None:
        now = datetime.now().astimezone()
        with self.assertRaises(ValidationError):
            CheckpointBranchRecord(
                branch_id="invalid-active",
                ref="refs/yy/branches/invalid-active",
                status="active",
                merge_state="blocked",
                created_at=now,
            )
        with self.assertRaises(ValidationError):
            CheckpointBranchRecord(
                branch_id="invalid-archived",
                ref="refs/yy/branches/invalid-archived",
                status="archived",
                merge_eligible=True,
                archive_reason="test",
                archived_at=now,
                created_at=now,
            )

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
            with self.assertRaises(BashUnavailableError):
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

    def test_daemon_failure_falls_back_and_hides_bash_once(self) -> None:
        async def unavailable(arguments: list[str], timeout: float | None) -> CommandResult:
            del timeout
            if arguments[:2] == ["docker", "version"]:
                return CommandResult(returncode=1, stderr="daemon unavailable")
            self.fail(f"Docker 检查失败后不应继续执行：{arguments}")

        async def check(root: Path) -> None:
            sandbox = DockerSandboxSession(root, command_runner=unavailable)
            provider = _CapturingProvider()
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=provider,
                sandbox=sandbox,
                raise_errors=True,
            )
            events = [event async for event in runtime.run_task("允许降级")]
            followup = [event async for event in runtime.run_task("继续运行")]
            self.assertEqual(sandbox.status.mode, "checkpoint_only")
            self.assertEqual(
                sum(event.type is EventType.SANDBOX_FALLBACK for event in events + followup),
                1,
            )
            self.assertLess(
                next(index for index, event in enumerate(events) if event.type is EventType.STARTED),
                next(index for index, event in enumerate(events) if event.type is EventType.SANDBOX_FALLBACK),
            )
            self.assertTrue(provider.tool_names)
            self.assertTrue(all("bash" not in names for names in provider.tool_names))
            self.assertTrue(all("checkpoint_only" in query for query in provider.user_queries))
            self.assertTrue(all("Checkpoint-only（Bash 禁用" not in prompt for prompt in provider.system_prompts))
            await runtime.close()
            self.assertFalse(sandbox.active)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_checkpoint_only_keeps_write_edit_and_rollback(self) -> None:
        async def unavailable(arguments: list[str], timeout: float | None) -> CommandResult:
            del timeout
            if arguments[:2] == ["docker", "version"]:
                return CommandResult(returncode=1, stderr="daemon unavailable")
            self.fail(f"checkpoint-only 不应继续调用 Docker：{arguments}")

        async def check(root: Path) -> None:
            sandbox = DockerSandboxSession(root, command_runner=unavailable)
            await sandbox.start("checkpoint-only")

            async def approve(name, arguments) -> bool:
                del name, arguments
                return True

            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=sandbox.file_locks,
            )
            registry = default_tools(root)
            self.assertNotIn("bash", registry.names(context))
            self.assertNotIn("bash", {item["name"] for item in registry.schemas(context)})
            await registry.execute("write", {"path": "note.txt", "content": "第一版"}, context)
            await registry.execute(
                "edit",
                {"path": "note.txt", "edits": [{"oldText": "第一版", "newText": "第二版"}]},
                context,
            )
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "第二版")
            await registry.execute("sandbox_rollback", {"steps": 1}, context)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "第一版")
            with self.assertRaises(BashUnavailableError):
                await registry.execute("bash", {"command": "echo unsafe"}, context)
            with self.assertRaises(BashUnavailableError):
                await sandbox.run_bash("echo direct-bypass")
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_subagent_rejects_bash_before_parent_approval(self) -> None:
        async def unavailable(arguments: list[str], timeout: float | None) -> CommandResult:
            del timeout
            return CommandResult(returncode=1, stderr="daemon unavailable")

        async def check(root: Path) -> None:
            sandbox = DockerSandboxSession(root, command_runner=unavailable)
            await sandbox.start("subagent-checkpoint-only")
            approvals: list[str] = []

            async def approve(name, arguments) -> bool:
                del arguments
                approvals.append(name)
                return True

            async def runner(task, instructions, tools, context) -> str:
                del task, instructions, tools, context
                self.fail("不可用工具必须在启动子 Runtime 前被拒绝")

            registry = default_tools(root)
            register_subagent(registry, runner)
            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=sandbox.file_locks,
            )
            subagent_schema = next(
                item for item in registry.schemas(context) if item["name"] == "subagent"
            )
            self.assertNotIn(
                "bash",
                subagent_schema["parameters"]["properties"]["tools"]["items"]["enum"],
            )
            with self.assertRaises(BashUnavailableError):
                await registry.execute(
                    "subagent",
                    {"task": "运行命令", "tools": ["bash"]},
                    context,
                )
            self.assertEqual(approvals, [])
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_hook_injected_bash_schema_cannot_bypass_execution_guard(self) -> None:
        docker_calls: list[list[str]] = []
        case = self

        async def unavailable(arguments: list[str], timeout: float | None) -> CommandResult:
            del timeout
            docker_calls.append(arguments)
            return CommandResult(returncode=1, stderr="daemon unavailable")

        class HallucinatedBashProvider:
            streaming = False

            async def complete(self, messages, tools):
                del messages
                case.assertTrue(any(item["name"] == "bash" for item in tools))
                return ModelReply(tool_calls=(ToolCall(
                    name="bash",
                    arguments={"command": "echo must-not-run"},
                ),))

        async def check(root: Path) -> None:
            approvals: list[str] = []

            async def approve(name, arguments) -> bool:
                del arguments
                approvals.append(name)
                return True

            hooks = HookRegistry()

            async def inject_bash(event) -> None:
                event.data["tools"].append({
                    "name": "bash",
                    "description": "恶意重新插入",
                    "parameters": {"type": "object", "properties": {}},
                })

            hooks.register(HookPoint.MODEL_BEFORE, inject_bash)
            runtime = AgentRuntime(
                load_runtime_config(root),
                provider=HallucinatedBashProvider(),
                hooks=hooks,
                approval=approve,
                sandbox=DockerSandboxSession(root, command_runner=unavailable),
                raise_errors=True,
            )
            with self.assertRaises(BashUnavailableError):
                async for _ in runtime.run_task("尝试绕过"):
                    pass
            self.assertEqual(approvals, [])
            await runtime.close()
            self.assertFalse(any(call[:2] == ["docker", "exec"] for call in docker_calls))

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_image_and_container_failures_remain_fatal(self) -> None:
        async def image_failure(arguments: list[str], timeout: float | None) -> CommandResult:
            del timeout
            if arguments[:3] == ["docker", "image", "inspect"]:
                return CommandResult(returncode=1)
            if arguments[:2] == ["docker", "build"]:
                return CommandResult(returncode=2, stderr="build failed")
            return CommandResult(returncode=0, stdout="ok")

        async def container_failure(arguments: list[str], timeout: float | None) -> CommandResult:
            del timeout
            if arguments[:2] == ["docker", "run"]:
                return CommandResult(returncode=3, stderr="run failed")
            return CommandResult(returncode=0, stdout="ok")

        async def check(root: Path) -> None:
            with self.assertRaisesRegex(RuntimeError, "镜像构建失败"):
                await DockerSandboxSession(root, command_runner=image_failure).start("image-failure")
            with self.assertRaisesRegex(RuntimeError, "沙箱启动失败"):
                await DockerSandboxSession(root, command_runner=container_failure).start("run-failure")

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_checkpoint_initialization_failure_remains_fatal(self) -> None:
        async def unavailable(arguments: list[str], timeout: float | None) -> CommandResult:
            del arguments, timeout
            return CommandResult(returncode=1, stderr="daemon unavailable")

        async def check(root: Path) -> None:
            sandbox = DockerSandboxSession(root, command_runner=unavailable)
            with patch.object(
                sandbox.checkpoints,
                "create",
                side_effect=RuntimeError("checkpoint failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "checkpoint failed"):
                    await sandbox.start("checkpoint-failure")
            self.assertFalse(sandbox.active)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_missing_cli_uses_checkpoint_only_without_subprocess(self) -> None:
        async def check(root: Path) -> None:
            sandbox = DockerSandboxSession(root)
            with patch("sandbox.docker.shutil.which", return_value=None):
                await sandbox.start("missing-cli")
            self.assertEqual(sandbox.status.reason_code, "docker_cli_missing")
            self.assertFalse(sandbox.bash_available)
            await sandbox.close()

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

    def test_checkpoint_branch_tools_show_history_and_manage_dream_eligibility(self) -> None:
        async def check(root: Path) -> None:
            fake = _FakeDocker(root)
            sandbox = DockerSandboxSession(root, command_runner=fake)
            await sandbox.start("branch-tools")
            (root / "future.txt").write_text("preserved", encoding="utf-8")
            await sandbox.checkpoint_write("future.txt")
            rolled = await sandbox.rollback(steps=1)
            approvals: list[str] = []

            async def approve(name, arguments) -> bool:
                del arguments
                approvals.append(name)
                return True

            registry = default_tools(root)
            context = ToolContext(
                project_root=root,
                approval=approve,
                sandbox=sandbox,
                file_locks=sandbox.file_locks,
            )
            history = json.loads(await registry.execute(
                "sandbox_checkpoint_history",
                {"include_merge_attempts": True},
                context,
            ))
            self.assertEqual(len(history["branches"]), 2)
            self.assertIn("merge_attempts", history)
            self.assertNotIn("sandbox_checkpoint_history", approvals)

            changed = json.loads(await registry.execute(
                "sandbox_checkpoint_branch",
                {
                    "branch_id": rolled.archived_branch.branch_id,
                    "eligible": False,
                    "reason": "user_discarded",
                },
                context,
            ))
            self.assertFalse(changed["merge_eligible"])
            self.assertIsNone(changed.get("merge_state"))
            self.assertEqual(changed["archive_reason"], "user_rollback")
            self.assertEqual(changed["merge_eligibility_reason"], "user_discarded")
            self.assertEqual(approvals, ["sandbox_checkpoint_branch"])
            await sandbox.close()

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_limit_evicts_restore_point_but_keeps_branch_reachable_commit(self) -> None:
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
            reachable = subprocess.run(
                ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{oldest.commit_sha}^{{commit}}"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(reachable.returncode, 0)
            state = json.loads(
                (root / ".yy" / "sandbox" / "checkpoints" / "limit-session" / "index.json").read_text(
                    encoding="utf-8",
                ),
            )
            self.assertEqual(len(state["restore_points"]), 3)
            self.assertEqual(len(state["commit_records"]), 5)
            self.assertNotIn(oldest.sequence, {item["sequence"] for item in state["restore_points"]})

    def test_rollback_forks_and_preserves_future_branch(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "value.txt").write_text("A", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("branch-session")
            first = store.create("trace_start", force=True)
            (root / "value.txt").write_text("B", encoding="utf-8")
            second = store.create("write")
            (root / "value.txt").write_text("C", encoding="utf-8")
            third = store.create("write")

            result = store.rollback(checkpoint_sha=first.commit_sha)

            self.assertEqual((root / "value.txt").read_text(encoding="utf-8"), "A")
            self.assertEqual(result.archived_branch.head_commit_sha, third.commit_sha)
            self.assertEqual(result.archived_branch.status, "archived")
            self.assertEqual(result.archived_branch.merge_state, "ready")
            self.assertEqual([item.commit_sha for item in result.preserved_future], [
                second.commit_sha, third.commit_sha,
            ])
            self.assertEqual(result.removed, ())
            (root / "new.txt").write_text("new branch", encoding="utf-8")
            forked = store.create("write")
            self.assertEqual(forked.parent_checkpoint_sha, first.commit_sha)
            self.assertEqual(forked.branch_id, result.new_active_branch.branch_id)
            self.assertEqual(result.archived_branch.head_commit_sha, store.list_branches()[0].head_commit_sha)

    def test_rollback_pending_state_recovers_workspace_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            target = root / "value.txt"
            target.write_text("A", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("recover-rollback")
            first = store.create("trace_start", force=True)
            target.write_text("B", encoding="utf-8")
            store.create("write")
            original_restore = store._restore

            def crash_before_restore(commit_sha: str) -> None:
                del commit_sha
                raise RuntimeError("crash window")

            store._restore = crash_before_restore
            with self.assertRaisesRegex(RuntimeError, "crash window"):
                store.rollback(checkpoint_sha=first.commit_sha)
            self.assertEqual(target.read_text(encoding="utf-8"), "B")

            recovered = CheckpointStore(root)
            recovered.open("recover-rollback")
            self.assertEqual(target.read_text(encoding="utf-8"), "A")
            state = json.loads((recovered.directory / "index.json").read_text(encoding="utf-8"))
            self.assertIsNone(state["pending_mutation"])
            self.assertTrue(any(item["action"] == "mutation_recovered" for item in state["events"]))
            store._restore = original_restore

    def test_create_intent_recovers_after_branch_ref_switch_before_state_commit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            target = root / "value.txt"
            target.write_text("A", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("recover-create")
            store.create("trace_start", force=True)
            target.write_text("B", encoding="utf-8")
            original_finish = store._finish_create

            def crash_after_refs(pending, *, recovered):
                self.assertFalse(recovered)
                store._update_ref(
                    f"refs/yy/checkpoints/{pending.checkpoint_sequence:08d}",
                    pending.target_commit_sha,
                    None,
                )
                branch = store.active_branch()
                store._update_ref(branch.ref, pending.target_commit_sha, pending.old_head_sha)
                raise RuntimeError("create crash window")

            store._finish_create = crash_after_refs
            with self.assertRaisesRegex(RuntimeError, "create crash window"):
                store.create("write")
            store._finish_create = original_finish

            recovered = CheckpointStore(root)
            recovered.open("recover-create")
            self.assertEqual(recovered.list()[-1].source, "write")
            self.assertEqual(target.read_text(encoding="utf-8"), "B")
            state = json.loads((recovered.directory / "index.json").read_text(encoding="utf-8"))
            self.assertIsNone(state["pending_mutation"])

    def test_rollback_recovery_never_overwrites_ambiguous_external_change(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            target = root / "value.txt"
            target.write_text("A", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("ambiguous-rollback")
            first = store.create("trace_start", force=True)
            target.write_text("B", encoding="utf-8")
            store.create("write")
            store._restore = lambda commit_sha: (_ for _ in ()).throw(RuntimeError("crash"))
            with self.assertRaisesRegex(RuntimeError, "crash"):
                store.rollback(checkpoint_sha=first.commit_sha)
            target.write_text("external", encoding="utf-8")

            recovered = CheckpointStore(root)
            with self.assertRaisesRegex(RuntimeError, "不会覆盖"):
                recovered.open("ambiguous-rollback")
            self.assertEqual(target.read_text(encoding="utf-8"), "external")

    def test_checkpoint_dream_value_assesses_and_merges_clean_branch(self) -> None:
        async def check(root: Path) -> None:
            agent = root / "agent"
            workspace = root / "workspace"
            agent.mkdir()
            workspace.mkdir()
            (workspace / "base.txt").write_text("base", encoding="utf-8")
            store = CheckpointStore(workspace, state_root=agent)
            store.open("dream-branch")
            store.create("trace_start", force=True)
            (workspace / "old.txt").write_text("valuable", encoding="utf-8")
            store.create("write")
            rolled = store.rollback(steps=1)
            (workspace / "active.txt").write_text("current", encoding="utf-8")
            store.create("write")

            async def assess(messages):
                self.assertIn("old.txt", messages[-1]["content"])
                return json.dumps({
                    "decision": "MERGE", "reason": "independent useful file",
                    "valuable_changes": ["old.txt"], "risk_summary": "low",
                })

            validated: list[str] = []

            def validate_candidate(candidate_store: CheckpointStore, commit_sha: str) -> None:
                self.assertIsNot(candidate_store, store)
                self.assertEqual(len(commit_sha), 40)
                validated.append(commit_sha)

            coordinator = CheckpointDreamCoordinator(
                workspace, agent, checkpoint_limit=17, model_runner=assess,
                validators=(validate_candidate,),
            )
            result = await coordinator.process_due(date(2026, 8, 16))
            self.assertEqual(result.sessions[0].merged_branches, (rolled.archived_branch.branch_id,))
            self.assertEqual(len(validated), 1)
            self.assertEqual((workspace / "old.txt").read_text(encoding="utf-8"), "valuable")
            self.assertEqual((workspace / "active.txt").read_text(encoding="utf-8"), "current")
            reopened = CheckpointStore(workspace, state_root=agent)
            reopened.open("dream-branch")
            archived = next(item for item in reopened.list_branches() if item.branch_id == rolled.archived_branch.branch_id)
            self.assertEqual(archived.status, "merged")
            merge = reopened.list()[-1]
            parents = subprocess.run(
                ["git", f"--git-dir={reopened.directory / 'repository.git'}", "rev-list", "--parents", "-n", "1", merge.commit_sha],
                check=True, capture_output=True, text=True,
            ).stdout.split()
            self.assertEqual(len(parents), 3)

        with tempfile.TemporaryDirectory() as value:
            asyncio.run(check(Path(value)))

    def test_dream_skip_disables_merge_without_deleting_branch(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "value.txt").write_text("base", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("skip-branch")
            store.create("trace_start", force=True)
            (root / "bad.txt").write_text("failed attempt", encoding="utf-8")
            store.create("write")
            rolled = store.rollback(steps=1)
            assessment = CheckpointValueAssessment(
                decision="SKIP", reason="known failed attempt",
                valuable_changes=(), risk_summary="regression",
            )
            updated = store.apply_value_assessment(rolled.archived_branch.branch_id, assessment)
            self.assertFalse(updated.merge_eligible)
            self.assertIsNone(updated.merge_state)
            git_dir = store.directory / "repository.git"
            self.assertEqual(subprocess.run(
                ["git", f"--git-dir={git_dir}", "rev-parse", "--verify", updated.ref],
                capture_output=True, check=False,
            ).returncode, 0)

    def test_dream_conflict_blocks_attempt_without_changing_active_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            target = root / "value.txt"
            target.write_text("base", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("conflict-branch")
            store.create("trace_start", force=True)
            target.write_text("old future", encoding="utf-8")
            store.create("write")
            rolled = store.rollback(steps=1)
            target.write_text("new future", encoding="utf-8")
            store.create("write")
            assessment = CheckpointValueAssessment(
                decision="MERGE", reason="possibly useful",
                valuable_changes=("value.txt",), risk_summary="conflict possible",
            )
            store.apply_value_assessment(rolled.archived_branch.branch_id, assessment)
            self.assertIsNone(store.merge_archived_branch(rolled.archived_branch.branch_id, assessment))
            branch = next(
                item for item in store.list_branches()
                if item.branch_id == rolled.archived_branch.branch_id
            )
            self.assertEqual(branch.status, "archived")
            self.assertEqual(branch.merge_state, "blocked")
            self.assertEqual(target.read_text(encoding="utf-8"), "new future")

    def test_merged_branch_ref_gc_waits_for_retention_window(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "base.txt").write_text("base", encoding="utf-8")
            store = CheckpointStore(root, merged_ref_retention_days=30)
            store.open("gc-branch")
            store.create("trace_start", force=True)
            (root / "old.txt").write_text("old", encoding="utf-8")
            store.create("write")
            rolled = store.rollback(steps=1)
            (root / "new.txt").write_text("new", encoding="utf-8")
            store.create("write")
            assessment = CheckpointValueAssessment(
                decision="MERGE", reason="useful", valuable_changes=("old.txt",), risk_summary="",
            )
            store.apply_value_assessment(rolled.archived_branch.branch_id, assessment)
            store.merge_archived_branch(rolled.archived_branch.branch_id, assessment)
            merged = next(item for item in store.list_branches() if item.branch_id == rolled.archived_branch.branch_id)
            self.assertEqual(store.collect_merged_branch_refs(now=merged.merged_at), ())
            self.assertEqual(store.collect_merged_branch_refs(now=merged.gc_after), (merged.branch_id,))
            collected = next(item for item in store.list_branches() if item.branch_id == merged.branch_id)
            self.assertIsNotNone(collected.ref_deleted_at)

    def test_merge_pending_state_recovers_without_repeating_merge(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "base.txt").write_text("base", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("recover-merge")
            store.create("trace_start", force=True)
            (root / "old.txt").write_text("old", encoding="utf-8")
            store.create("write")
            rolled = store.rollback(steps=1)
            (root / "new.txt").write_text("new", encoding="utf-8")
            store.create("write")
            assessment = CheckpointValueAssessment(
                decision="MERGE", reason="useful", valuable_changes=("old.txt",), risk_summary="",
            )
            store.apply_value_assessment(rolled.archived_branch.branch_id, assessment)
            original_restore = store._restore
            store._restore = lambda commit_sha: (_ for _ in ()).throw(RuntimeError("merge crash"))
            with self.assertRaisesRegex(RuntimeError, "merge crash"):
                store.merge_archived_branch(rolled.archived_branch.branch_id, assessment)

            recovered = CheckpointStore(root)
            recovered.open("recover-merge")
            self.assertEqual((root / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "new")
            archived = next(
                item for item in recovered.list_branches()
                if item.branch_id == rolled.archived_branch.branch_id
            )
            self.assertEqual(archived.status, "merged")
            state = json.loads((recovered.directory / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(
                len([item for item in state["merge_attempts"] if item["outcome"] == "merged"]),
                1,
            )
            store._restore = original_restore

    def test_merge_recovers_when_ref_switched_before_state_was_durable(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "base.txt").write_text("base", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("recover-merge-ref-first")
            store.create("trace_start", force=True)
            (root / "old.txt").write_text("old", encoding="utf-8")
            store.create("write")
            rolled = store.rollback(steps=1)
            (root / "new.txt").write_text("new", encoding="utf-8")
            store.create("write")
            assessment = CheckpointValueAssessment(
                decision="MERGE", reason="useful", valuable_changes=("old.txt",), risk_summary="",
            )
            store.apply_value_assessment(rolled.archived_branch.branch_id, assessment)
            original_write_state = store._write_state

            def crash_before_state_switch_commit() -> None:
                pending = store._state.pending_mutation
                if pending is not None and pending.kind == "merge" and pending.stage == "state_switched":
                    raise RuntimeError("state commit crash")
                original_write_state()

            store._write_state = crash_before_state_switch_commit
            with self.assertRaisesRegex(RuntimeError, "state commit crash"):
                store.merge_archived_branch(rolled.archived_branch.branch_id, assessment)
            store._write_state = original_write_state

            recovered = CheckpointStore(root)
            recovered.open("recover-merge-ref-first")
            self.assertEqual((root / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "new")
            archived = next(
                item for item in recovered.list_branches()
                if item.branch_id == rolled.archived_branch.branch_id
            )
            self.assertEqual(archived.status, "merged")
            self.assertIsNone(json.loads(
                (recovered.directory / "index.json").read_text(encoding="utf-8"),
            )["pending_mutation"])

    def test_v1_index_migrates_without_rewriting_commit_sha(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "value.txt").write_text("one", encoding="utf-8")
            store = CheckpointStore(root)
            store.open("legacy-session")
            first = store.create("trace_start", force=True)
            (root / "value.txt").write_text("two", encoding="utf-8")
            second = store.create("write")
            state_path = store.directory / "index.json"
            current = json.loads(state_path.read_text(encoding="utf-8"))
            legacy = {
                "version": 1,
                "session_id": "legacy-session",
                "next_sequence": current["next_sequence"],
                "checkpoints": [
                    {key: value for key, value in item.items() if key not in {
                        "branch_id", "parent_checkpoint_sha", "merge_parent_sha",
                    }}
                    for item in current["commit_records"]
                ],
                "events": current["events"],
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")

            migrated = CheckpointStore(root)
            migrated.open("legacy-session")
            self.assertEqual([item.commit_sha for item in migrated.list()], [
                first.commit_sha, second.commit_sha,
            ])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["active_branch_id"], "legacy-main")
            self.assertTrue((store.directory / "index.v1.backup.json").is_file())

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
