"""Harness 自进化的错误快照、隔离 worktree 与可扩展验证流水线。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from Agent import AgentRuntime, HookRegistry, ModelRetryPolicy, RuntimeConfig, RuntimeFailure
from tools import AsyncToolRegistry


_SECRET_KEYS = {"api_key", "authorization", "access_token", "token", "secret", "password"}
_SOURCE_PATH = re.compile(r'File "([^"]+)"')


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _sanitize(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """递归移除凭据，同时保留复现所需的完整结构。"""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SECRET_KEYS else _sanitize(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        result = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]", result)
        result = re.sub(r"(?i)(api[_-]?key|access[_-]?token|password)(\s*[:=]\s*)[^\s,;\"']+", r"\1\2[REDACTED]", result)
        result = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", result)
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class ErrorSnapshotWriter:
    """创建无索引、只按哈希命名的完整错误复现 JSONL。"""

    def __init__(self, project_root: Path, *, secrets: tuple[str, ...] = ()) -> None:
        self.directory = project_root.resolve() / "tests" / "error"
        self.secrets = tuple(secret for secret in secrets if secret)

    def capture(
        self,
        *,
        task: str,
        session_id: str,
        failure: RuntimeFailure,
        session_records: list[dict[str, Any]],
        session_file: str = "",
    ) -> Path:
        """原子写入初始错误现场，并返回纯哈希文件路径。"""
        now = datetime.now().astimezone().isoformat()
        digest = hashlib.sha256(
            f"{now}:{session_id}:{task}:{type(failure.error).__name__}:{uuid4().hex}".encode("utf-8")
        ).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{digest}.jsonl"
        response_excerpt = getattr(failure.error, "response_excerpt", "")
        source_paths = list(dict.fromkeys(_SOURCE_PATH.findall(failure.traceback_text)))
        records: list[dict[str, Any]] = [{
            "record_type": "incident",
            "incident_id": digest,
            "timestamp": _timestamp(),
            "session_id": session_id,
            "session_file": session_file,
            "project_root": str(self.directory.parents[1]),
            "user_question": task,
            "model": failure.model,
            "retry_history": failure.retry_history,
        }]
        records.extend({
            **record,
            "record_type": "session_record",
        } for record in session_records)
        records.extend({
            **message,
            "record_type": "message",
            "captured_at": _timestamp(),
        } for message in failure.messages)
        records.append({
            "record_type": "tool_schema",
            "timestamp": _timestamp(),
            "tools": failure.tools,
        })
        records.append({
            "record_type": "error",
            "timestamp": _timestamp(),
            "category": failure.category,
            "error_type": type(failure.error).__name__,
            "message": str(failure.error) or type(failure.error).__name__,
            "traceback": failure.traceback_text,
            "source_paths": source_paths,
            "response_excerpt": response_excerpt,
        })
        payload = "".join(
            json.dumps(_sanitize(record, self.secrets), ensure_ascii=False) + "\n"
            for record in records
        )
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
        return path

    def append_event(self, path: Path, record_type: str, **data: Any) -> None:
        """向既有快照追加确认、演进、测试或清理事件。"""
        resolved = path.resolve()
        if resolved.parent != self.directory or not re.fullmatch(r"[0-9a-f]{64}\.jsonl", resolved.name):
            raise ValueError("错误快照路径不属于 tests/error 或文件名不是 SHA-256")
        record = {"record_type": record_type, "timestamp": _timestamp(), **data}
        with resolved.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(_sanitize(record, self.secrets), ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class HarnessEvolutionRequest:
    """一次经用户确认的隔离诊断请求。"""

    project_root: Path
    incident_id: str
    snapshot_path: Path
    task: str
    config: RuntimeConfig


@dataclass(frozen=True)
class HarnessEvolutionResult:
    """Harness 流水线的最终状态。"""

    status: str
    message: str
    worktree_path: str = ""
    branch: str = ""
    merged: bool = False


class _NoMemory:
    """空 Coding Runtime 的显式无持久化占位对象。"""


def create_coding_runtime(config: RuntimeConfig, worktree_root: Path) -> AgentRuntime:
    """复用正式 AgentRuntime 类创建无 Tool、无 Skill、无 Memory 的诊断实例。"""
    isolated = replace(
        config,
        project_root=worktree_root.resolve(),
        stream=False,
        compression_threshold_tokens=0,
    )
    return AgentRuntime(
        isolated,
        tools=AsyncToolRegistry(),
        memory=_NoMemory(),
        hooks=HookRegistry(),
        enable_context_processing=False,
        enable_subagent=False,
        retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=2),
        raise_errors=True,
    )


class HarnessEvolutionRunner:
    """管理 worktree，并为未来 Coding Tool/Skill 预留测试与合并路径。"""

    def __init__(
        self,
        writer: ErrorSnapshotWriter,
        *,
        runtime_factory: Callable[[RuntimeConfig, Path], AgentRuntime] = create_coding_runtime,
    ) -> None:
        self.writer = writer
        self.runtime_factory = runtime_factory

    async def run(self, request: HarnessEvolutionRequest) -> HarnessEvolutionResult:
        root = request.project_root.resolve()
        clean = await self._git(root, "status", "--porcelain", "--untracked-files=all")
        if clean.stdout.strip():
            message = "主 worktree 存在未提交修改，Harness 已停止且不会 stash 用户内容"
            self.writer.append_event(request.snapshot_path, "evolution", status="dirty_worktree", message=message)
            return HarnessEvolutionResult("dirty_worktree", message)
        branch_result = await self._git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            message = "当前不在可识别分支上，Harness 无法安全合并"
            self.writer.append_event(request.snapshot_path, "evolution", status="detached_head", message=message)
            return HarnessEvolutionResult("detached_head", message)
        base = (await self._git(root, "rev-parse", "HEAD")).stdout.strip()
        branch = f"harness-evolution/{request.incident_id[:16]}"
        worktree = (root / ".yy" / "harness-evolution" / "worktrees" / request.incident_id).resolve()
        parent = (root / ".yy" / "harness-evolution" / "worktrees").resolve()
        if parent not in worktree.parents:
            raise ValueError("Harness worktree 路径越界")
        parent.mkdir(parents=True, exist_ok=True)
        await self._git(root, "worktree", "add", "-b", branch, str(worktree), base)
        keep_branch = False
        try:
            self.writer.append_event(
                request.snapshot_path,
                "evolution",
                status="worktree_created",
                worktree_path=str(worktree),
                branch=branch,
                base_commit=base,
            )
            runtime = self.runtime_factory(request.config, worktree)
            diagnostic_task = (
                "你是一个尚未接入 Coding Tool 与 Skill 的诊断 Agent。"
                "请仅根据下面的完整错误快照分析可能原因，不要声称已经读取或修改仓库。\n\n"
                + request.snapshot_path.read_text(encoding="utf-8")
            )
            try:
                result = await runtime.run(diagnostic_task)
                diagnostic = result.answer
            except Exception as exc:
                diagnostic = f"诊断 Agent 失败：{str(exc) or type(exc).__name__}"
            finally:
                await runtime.close()
            self.writer.append_event(request.snapshot_path, "evolution", status="diagnosed", diagnostic=diagnostic)
            changes = (await self._git(worktree, "status", "--porcelain", "--untracked-files=all")).stdout
            if not changes.strip():
                message = "Coding Agent 当前没有 Tool/Skill，未产生代码变更"
                self.writer.append_event(
                    request.snapshot_path,
                    "evolution",
                    status="no_code_changes",
                    message=message,
                    worktree_path=str(worktree),
                )
                return HarnessEvolutionResult("no_code_changes", message, str(worktree), branch)

            forbidden = _forbidden_changed_paths(changes)
            if forbidden:
                message = f"Coding Agent 修改了禁止路径：{forbidden[0]}"
                self.writer.append_event(
                    request.snapshot_path,
                    "evolution",
                    status="forbidden_changes",
                    message=message,
                    forbidden_paths=forbidden,
                )
                return HarnessEvolutionResult("forbidden_changes", message, str(worktree), branch)

            self.writer.append_event(request.snapshot_path, "evolution", status="changes_detected", git_status=changes)
            await self._git(worktree, "add", "--intent-to-add", "--all")
            tests = await self._run_tests(worktree, request.snapshot_path)
            if not tests:
                return HarnessEvolutionResult("tests_failed", "新版本测试失败，已丢弃隔离 worktree", str(worktree), branch)
            await self._git(worktree, "add", "--all")
            await self._git(
                worktree,
                "-c", "user.name=Yuan Ye Harness",
                "-c", "user.email=harness@local.invalid",
                "commit", "-m", f"Harness evolution {request.incident_id[:12]}",
            )
            if (await self._git(root, "status", "--porcelain", "--untracked-files=all")).stdout.strip():
                keep_branch = True
                message = "验证后主 worktree 发生变化，已拒绝自动合并并保留临时分支"
                self.writer.append_event(request.snapshot_path, "evolution", status="main_changed", message=message, branch=branch)
                return HarnessEvolutionResult("main_changed", message, str(worktree), branch)
            if (await self._git(root, "rev-parse", "HEAD")).stdout.strip() != base:
                keep_branch = True
                message = "验证期间主分支 HEAD 已变化，已拒绝自动合并并保留临时分支"
                self.writer.append_event(request.snapshot_path, "evolution", status="main_changed", message=message, branch=branch)
                return HarnessEvolutionResult("main_changed", message, str(worktree), branch)
            await self._git(root, "merge", "--ff-only", branch)
            self.writer.append_event(request.snapshot_path, "evolution", status="merged", branch=branch)
            return HarnessEvolutionResult("merged", "修复已合并，下次启动生效", str(worktree), branch, True)
        finally:
            cleanup = await self._cleanup(root, worktree, branch, keep_branch=keep_branch)
            self.writer.append_event(
                request.snapshot_path,
                "evolution",
                status="cleanup",
                former_worktree_path=str(worktree),
                branch=branch,
                branch_preserved=keep_branch,
                **cleanup,
            )

    async def _run_tests(self, worktree: Path, snapshot_path: Path) -> bool:
        commands = [
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "Agent", "bootstrap", "context_process", "memory", "prompt", "tools", "run_ui", "tests", "harness-evolution", "run.py"],
            ["uv", "lock", "--check"],
            ["git", "diff", "--check"],
        ]
        for command in commands:
            result = await self._command(worktree, command, check=False, timeout=1200)
            self.writer.append_event(
                snapshot_path,
                "test",
                command=command,
                returncode=result.returncode,
                stdout=result.stdout[-65536:],
                stderr=result.stderr[-65536:],
            )
            if result.returncode != 0:
                return False
        return True

    async def _cleanup(self, root: Path, worktree: Path, branch: str, *, keep_branch: bool) -> dict[str, Any]:
        remove = await self._git(root, "worktree", "remove", "--force", str(worktree), check=False)
        branch_result = _CommandResult(0, "", "")
        if not keep_branch:
            branch_result = await self._git(root, "branch", "-D", branch, check=False)
        return {
            "worktree_remove_code": remove.returncode,
            "worktree_remove_error": remove.stderr[-4096:],
            "branch_remove_code": branch_result.returncode,
            "branch_remove_error": branch_result.stderr[-4096:],
        }

    async def _git(self, directory: Path, *arguments: str, check: bool = True) -> "_CommandResult":
        return await self._command(directory, ["git", *arguments], check=check)

    @staticmethod
    async def _command(
        directory: Path,
        command: list[str],
        *,
        check: bool = True,
        timeout: float = 120,
    ) -> "_CommandResult":
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError(f"命令执行超时：{' '.join(command)}")
        result = _CommandResult(
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"命令执行失败：{' '.join(command)}\n{result.stderr or result.stdout}")
        return result


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _forbidden_changed_paths(status: str) -> list[str]:
    """拒绝运行状态、Git 元数据和本机凭据文件进入自动提交。"""
    forbidden: list[str] = []
    for line in status.splitlines():
        path = line[3:].strip().strip('"') if len(line) >= 4 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip('"')
        normalized = path.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        lowered = normalized.lower()
        name = Path(normalized).name.lower()
        if (
            lowered == ".git"
            or lowered.startswith(".git/")
            or lowered == ".yy"
            or lowered.startswith(".yy/")
            or lowered.startswith("tests/error/")
            or name.startswith(".env")
            or name in {"settings.local.json", "config.ini"}
        ):
            forbidden.append(normalized)
    return forbidden
