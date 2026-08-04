"""Harness 自进化的错误快照、隔离 worktree 与可扩展验证流水线。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from Agent import AgentRuntime, EventType, ExtensionLoader, ModelRetryPolicy, RuntimeConfig, RuntimeFailure
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from Agent.runtime.subagent import RuntimeSubagentRunner
from memory import HarnessLongTermMemory, HarnessMemoryUpdate, MemoryStore
from prompt import compose_harness_memory_messages
from sandbox import SandboxSessionProtocol
from skill import SkillService
from tool import (
    AsyncToolRegistry,
    default_tools,
    register_subagent,
)
from tools import SkillReadTool, WebFetchTool, WebSearchTool


_SECRET_KEYS = {
    "api_key",
    "web_search_api_key",
    "reference_embedding_api_key",
    "authorization",
    "access_token",
    "token",
    "secret",
    "password",
}
_SOURCE_PATH = re.compile(r'File "([^"]+)"')
_CODING_BASE_TOOL_NAMES = (
    "read_file",
    "search_workspace",
    "edit",
    "write",
    "bash",
    "sandbox_rollback",
    "web_fetch",
)


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
        question_indexes = [
            index
            for index, message in enumerate(failure.messages)
            if message.get("role") == "user" and message.get("content") == task
        ]
        incident: dict[str, Any] = {
            "record_type": "incident",
            "incident_id": digest,
            "timestamp": _timestamp(),
            "session_id": session_id,
            "session_file": session_file,
            "project_root": str(self.directory.parents[1]),
            "model": failure.model,
            "retry_history": failure.retry_history,
        }
        if question_indexes:
            incident["user_question_message_index"] = question_indexes[-1]
        else:
            incident["user_question"] = task
        records: list[dict[str, Any]] = [{
            **incident,
        }]
        records.extend(_session_audit(record, index) for index, record in enumerate(session_records))
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


class HarnessEvolutionRequest(BaseModel):
    """一次经用户确认的隔离诊断请求。"""

    model_config = ConfigDict(frozen=True, strict=True)

    project_root: Path
    incident_id: str
    snapshot_path: Path
    task: str
    config: RuntimeConfig


class HarnessEvolutionResult(BaseModel):
    """Harness 流水线的最终状态。"""

    model_config = ConfigDict(frozen=True, strict=True)

    status: str
    message: str
    worktree_path: str = ""
    branch: str = ""
    merged: bool = False


class CodeSessionRecord(BaseModel):
    """一个持续 `/code` 会话在隔离 worktree 中的可审计状态。"""

    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str
    coding_memory_session_id: str
    source_root: Path
    worktree_path: Path
    branch: str
    base_commit: str
    status: str = "active"
    last_verified_commit: str
    verified_turns: int = 0
    audit_path: Path


class CodeTurnResult(BaseModel):
    """一次 Coding 需求经过生成、修复、验证和临时提交后的结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str
    status: str
    message: str
    test_file: str
    attempts: int = Field(ge=1)
    commit: str = ""
    diagnostic: str = ""


class CodeFinalizeResult(BaseModel):
    """`/exit` 的合并或拒绝结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    code_session_id: str
    status: str
    message: str
    merged: bool = False
    stay_in_code_mode: bool = False
    worktree_path: str = ""
    branch: str = ""


class CodeAuditWriter:
    """把 Coding 模式生命周期追加到 Agent Home，不污染普通 Session。"""

    def __init__(self, agent_root: Path) -> None:
        self.directory = agent_root.resolve() / ".yy" / "harness-evolution" / "code"
        self.directory.mkdir(parents=True, exist_ok=True)

    def create(self, code_session_id: str, **data: Any) -> Path:
        path = self.directory / f"{code_session_id}.jsonl"
        if not re.fullmatch(r"[0-9a-f]{32}\.jsonl", path.name):
            raise ValueError("Code Session ID 必须是 32 位小写十六进制")
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text("", encoding="utf-8")
        temporary.replace(path)
        self.append_event(path, "code_session_started", **data)
        return path

    def append_event(self, path: Path, record_type: str, **data: Any) -> None:
        resolved = path.resolve()
        if resolved.parent != self.directory or not re.fullmatch(r"[0-9a-f]{32}\.jsonl", resolved.name):
            raise ValueError("Coding 审计路径不属于 Agent Home")
        try:
            sequence = sum(
                1 for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip()
            ) + 1
        except FileNotFoundError:
            sequence = 1
        record = {
            "version": 1,
            "sequence": sequence,
            "record_type": record_type,
            "timestamp": _timestamp(),
            **data,
        }
        with resolved.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(_sanitize(record), ensure_ascii=False) + "\n")


class CodeSessionController:
    """持续持有 worktree 与同一个 AgentRuntime 的 `/code` 控制器。"""

    _ALLOWED_PREFIXES = ("extension/hook/", "tests/extensions/")
    _ALLOWED_FILES = {"extension/README.md"}
    _BASE_FILES = {
        f"extension/hook/{point}/{point}.py"
        for point in (
            "trace_start", "trace_end", "turn_start", "turn_end",
            "model_before", "model_during", "model_after",
            "tool_before", "tool_during", "tool_after",
        )
    }

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        runtime_factory: Callable[[RuntimeConfig, Path], AgentRuntime] | None = None,
        memory_provider_factory: Callable[[RuntimeConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self.runtime_factory = runtime_factory
        self.memory_provider_factory = memory_provider_factory
        self.audit = CodeAuditWriter(config.agent_root)
        self.record: CodeSessionRecord | None = None
        self.runtime: AgentRuntime | None = None
        self._requirements: list[str] = []

    async def start(self, source_root: Path | None = None) -> CodeSessionRecord:
        if self.record is not None:
            raise RuntimeError("Coding Session 已经启动")
        root = (source_root or self.config.coding_source_root or Path(__file__).resolve().parents[1]).resolve()
        await self._require_clean_source(root)
        branch_name = (await self._git(root, "symbolic-ref", "--quiet", "--short", "HEAD")).stdout.strip()
        if not branch_name:
            raise RuntimeError("Yuan Ye 源码仓库处于 detached HEAD，不能启动 /code")
        base = (await self._git(root, "rev-parse", "HEAD")).stdout.strip()
        code_id = uuid4().hex
        source_hash = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:16]
        worktree_parent = (
            self.config.agent_root / ".yy" / "harness-evolution" / "worktrees" / source_hash
        ).resolve()
        worktree = (worktree_parent / code_id).resolve()
        if worktree_parent not in worktree.parents:
            raise ValueError("Coding worktree 路径越界")
        existing = (
            sorted(path for path in worktree_parent.iterdir() if path.is_dir())
            if worktree_parent.is_dir()
            else []
        )
        if existing:
            raise RuntimeError(
                "检测到该源码仓库尚未清理的 Coding worktree；为避免并发修改，"
                f"请先检查或处理：{existing[0]}"
            )
        worktree_parent.mkdir(parents=True, exist_ok=True)
        branch = f"harness-code/{code_id}"
        await self._git(root, "worktree", "add", "-b", branch, str(worktree), base)
        try:
            factory = self.runtime_factory or create_coding_runtime
            runtime = factory(self.config, worktree)
            if not _runtime_targets_worktree(runtime, worktree):
                await runtime.close()
                raise RuntimeError("Coding Runtime 没有指向隔离 worktree")
            audit_path = self.audit.create(
                code_id,
                source_root=str(root),
                worktree_path=str(worktree),
                branch=branch,
                base_commit=base,
                source_branch=branch_name,
                coding_memory_session_id=str(getattr(runtime, "coding_session_id", "")),
            )
            self.runtime = runtime
            self.record = CodeSessionRecord(
                code_session_id=code_id,
                coding_memory_session_id=str(getattr(runtime, "coding_session_id", "")),
                source_root=root,
                worktree_path=worktree,
                branch=branch,
                base_commit=base,
                last_verified_commit=base,
                audit_path=audit_path,
            )
            return self.record
        except Exception:
            await self._git(root, "worktree", "remove", "--force", str(worktree), check=False)
            await self._git(root, "branch", "-D", branch, check=False)
            raise

    async def run_turn(self, task: str) -> CodeTurnResult:
        record, runtime = self._active()
        requirement = task.strip()
        if not requirement:
            raise ValueError("Coding 需求不能为空")
        test_id = hashlib.sha256(
            f"{record.code_session_id}:{record.verified_turns}:{requirement}:{uuid4().hex}".encode("utf-8")
        ).hexdigest()[:8]
        slug = _code_slug(requirement)
        test_file = f"tests/extensions/test_{slug}_{test_id}.py"
        self._requirements.append(requirement)
        self.audit.append_event(
            record.audit_path,
            "code_turn_started",
            task=requirement,
            required_test_file=test_file,
        )
        diagnostic = ""
        feedback = ""
        for attempt in range(1, 5):
            prompt = self._turn_prompt(requirement, test_file, attempt, feedback)
            self.audit.append_event(
                record.audit_path,
                "code_generation" if attempt == 1 else "code_auto_repair",
                attempt=attempt,
                prompt=prompt,
            )
            try:
                chunks: list[str] = []
                async for event in runtime.run_task(prompt, session_id=record.coding_memory_session_id):
                    if event.type is EventType.FINAL:
                        chunks.append(str(event.payload.get("answer", "")))
                diagnostic = chunks[-1] if chunks else ""
            except Exception as exc:
                feedback = f"Coding Runtime 执行失败：{str(exc) or type(exc).__name__}"
                self.audit.append_event(
                    record.audit_path,
                    "code_runtime_failed",
                    attempt=attempt,
                    message=feedback,
                )
                if attempt < 4:
                    continue
                return self._failed_turn(record, test_file, attempt, feedback, diagnostic)

            validation = await self._validate_and_test(record, test_file)
            if validation["passed"]:
                await self._git(record.worktree_path, "add", "--all")
                await self._git(
                    record.worktree_path,
                    "-c", "user.name=Yuan Ye Harness",
                    "-c", "user.email=harness@local.invalid",
                    "commit", "-m", f"Extension: {slug} ({record.verified_turns + 1})",
                )
                commit = (await self._git(record.worktree_path, "rev-parse", "HEAD")).stdout.strip()
                self.record = record.model_copy(update={
                    "last_verified_commit": commit,
                    "verified_turns": record.verified_turns + 1,
                    "status": "active",
                })
                self.audit.append_event(
                    record.audit_path,
                    "code_turn_verified",
                    attempt=attempt,
                    commit=commit,
                    test_file=test_file,
                    diagnostic=diagnostic,
                )
                return CodeTurnResult(
                    code_session_id=record.code_session_id,
                    status="verified",
                    message="扩展代码和测试已通过验证，并提交到隔离临时分支。",
                    test_file=test_file,
                    attempts=attempt,
                    commit=commit,
                    diagnostic=diagnostic,
                )
            feedback = str(validation["feedback"])
            self.audit.append_event(
                record.audit_path,
                "code_tests_failed",
                attempt=attempt,
                feedback=feedback,
            )
        self.record = record.model_copy(update={"status": "unverified"})
        return self._failed_turn(record, test_file, 4, feedback, diagnostic)

    async def finalize(self) -> CodeFinalizeResult:
        record, runtime = self._active()
        status = (await self._git(
            record.worktree_path, "status", "--porcelain", "--untracked-files=all"
        )).stdout.strip()
        head = (await self._git(record.worktree_path, "rev-parse", "HEAD")).stdout.strip()
        if status or head != record.last_verified_commit or record.status == "unverified":
            return CodeFinalizeResult(
                code_session_id=record.code_session_id,
                status="unverified_changes",
                message="worktree 存在未验证修改，不能合并；请继续修复后再输入 /exit。",
                stay_in_code_mode=True,
                worktree_path=str(record.worktree_path),
                branch=record.branch,
            )
        await runtime.close()
        self.runtime = None
        if record.last_verified_commit == record.base_commit:
            await self._cleanup(record, keep_branch=False)
            self.record = None
            return CodeFinalizeResult(
                code_session_id=record.code_session_id,
                status="no_changes",
                message="本次 Coding Session 没有已验证改动，已清理并返回普通聊天。",
            )
        await self._require_clean_source(record.source_root)
        current_head = (await self._git(record.source_root, "rev-parse", "HEAD")).stdout.strip()
        if current_head != record.base_commit:
            self.audit.append_event(
                record.audit_path, "code_merge_refused",
                status="main_changed", current_head=current_head,
            )
            self.record = record.model_copy(update={"status": "main_changed"})
            return CodeFinalizeResult(
                code_session_id=record.code_session_id,
                status="main_changed",
                message="源码仓库 HEAD 已变化，已保留临时分支与 worktree，未执行合并。",
                stay_in_code_mode=False,
                worktree_path=str(record.worktree_path),
                branch=record.branch,
            )
        await self._git(record.source_root, "merge", "--ff-only", record.branch)
        changed_files = [
            line for line in (
                await self._git(
                    record.source_root, "diff", "--name-only",
                    f"{record.base_commit}..{record.last_verified_commit}",
                )
            ).stdout.splitlines() if line.strip()
        ]
        self.audit.append_event(
            record.audit_path, "code_merged",
            commit=record.last_verified_commit, changed_files=changed_files,
        )
        try:
            await self._update_long_term(record, changed_files)
        except Exception as exc:
            self.audit.append_event(
                record.audit_path, "long_term_memory_failed",
                message=str(exc) or type(exc).__name__,
            )
        await self._cleanup(record, keep_branch=False)
        self.record = None
        return CodeFinalizeResult(
            code_session_id=record.code_session_id,
            status="merged",
            message="已 fast-forward 合并验证通过的扩展；重启 Gateway 后生效。",
            merged=True,
        )

    async def abort(self) -> CodeFinalizeResult:
        record, runtime = self._active()
        await runtime.close()
        self.runtime = None
        await self._cleanup(record, keep_branch=False)
        self.audit.append_event(record.audit_path, "code_session_aborted")
        self.record = None
        return CodeFinalizeResult(
            code_session_id=record.code_session_id,
            status="aborted",
            message="已放弃 Coding Session 并清理隔离 worktree。",
        )

    async def _validate_and_test(self, record: CodeSessionRecord, test_file: str) -> dict[str, Any]:
        status = (await self._git(
            record.worktree_path, "status", "--porcelain", "--untracked-files=all"
        )).stdout
        changed = _status_paths(status)
        forbidden = [
            path for path in changed
            if path not in self._ALLOWED_FILES and not path.startswith(self._ALLOWED_PREFIXES)
        ]
        removed_base = [
            path for path in self._BASE_FILES
            if not (record.worktree_path / path).is_file()
        ]
        extension_changes = [path for path in changed if path.startswith("extension/hook/")]
        failures: list[str] = []
        if forbidden:
            await self._rollback_unverified(record)
            return {"passed": False, "feedback": f"检测到越界修改并已回滚：{', '.join(forbidden)}"}
        if removed_base:
            await self._rollback_unverified(record)
            return {"passed": False, "feedback": f"基础 Hook 文件不能删除并已回滚：{', '.join(removed_base)}"}
        if not extension_changes:
            failures.append("本轮没有 Extension Hook 代码变更")
        if test_file not in changed or not (record.worktree_path / test_file).is_file():
            failures.append(f"必须生成控制器指定的测试文件：{test_file}")
        if failures:
            return {"passed": False, "feedback": "\n".join(failures)}
        contract = _validate_changed_extensions(record.worktree_path, extension_changes)
        if contract:
            return {"passed": False, "feedback": contract}
        commands = [
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q", test_file],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q", "tests/extensions"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "extension"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            ["uv", "lock", "--check"],
            ["git", "diff", "--check"],
        ]
        outputs: list[str] = []
        for command in commands:
            result = await self._command(record.worktree_path, command, check=False, timeout=1200)
            self.audit.append_event(
                record.audit_path, "code_test",
                command=command, returncode=result.returncode,
                stdout=result.stdout[-65536:], stderr=result.stderr[-65536:],
            )
            if result.returncode != 0:
                outputs.append(
                    f"$ {' '.join(command)}\nexit={result.returncode}\n"
                    f"{result.stdout[-12000:]}\n{result.stderr[-12000:]}"
                )
                return {"passed": False, "feedback": "\n".join(outputs)}
        return {"passed": True, "feedback": ""}

    async def _rollback_unverified(self, record: CodeSessionRecord) -> None:
        await self._git(record.worktree_path, "reset", "--hard", record.last_verified_commit)
        await self._git(record.worktree_path, "clean", "-fd")

    async def _update_long_term(self, record: CodeSessionRecord, changed_files: list[str]) -> None:
        request = HarnessEvolutionRequest(
            project_root=record.source_root,
            incident_id=record.code_session_id,
            snapshot_path=record.audit_path,
            task="\n\n".join(self._requirements),
            config=self.config,
        )
        runner = HarnessEvolutionRunner(
            self.audit,  # type: ignore[arg-type]
            runtime_factory=self.runtime_factory or create_coding_runtime,
            memory_provider_factory=self.memory_provider_factory,
        )
        await runner._update_long_term_memory(
            request,
            record.source_root,
            diagnostic="通过交互式 /code Coding Session 创建并验证 Extension。",
            commit_sha=record.last_verified_commit,
            changed_files=changed_files,
        )

    async def _cleanup(self, record: CodeSessionRecord, *, keep_branch: bool) -> None:
        remove = await self._git(
            record.source_root, "worktree", "remove", "--force",
            str(record.worktree_path), check=False,
        )
        branch = _CommandResult(returncode=0, stdout="", stderr="")
        if not keep_branch:
            branch = await self._git(record.source_root, "branch", "-D", record.branch, check=False)
        self.audit.append_event(
            record.audit_path, "code_cleanup",
            worktree_remove_code=remove.returncode,
            branch_remove_code=branch.returncode,
            branch_preserved=keep_branch,
        )

    async def _require_clean_source(self, root: Path) -> None:
        inside = await self._git(root, "rev-parse", "--is-inside-work-tree", check=False)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeError(f"Yuan Ye 源码目录不是 Git 仓库：{root}")
        status = await self._git(root, "status", "--porcelain", "--untracked-files=all")
        if status.stdout.strip():
            raise RuntimeError("Yuan Ye 源码仓库存在未提交修改；/code 不会 stash 或覆盖这些内容")

    def _active(self) -> tuple[CodeSessionRecord, AgentRuntime]:
        if self.record is None or self.runtime is None:
            raise RuntimeError("没有活动的 Coding Session")
        return self.record, self.runtime

    def _failed_turn(
        self,
        record: CodeSessionRecord,
        test_file: str,
        attempts: int,
        feedback: str,
        diagnostic: str,
    ) -> CodeTurnResult:
        self.audit.append_event(
            record.audit_path, "code_turn_unverified",
            attempts=attempts, feedback=feedback, diagnostic=diagnostic,
        )
        if self.record is not None:
            self.record = self.record.model_copy(update={"status": "unverified"})
        return CodeTurnResult(
            code_session_id=record.code_session_id,
            status="unverified",
            message=f"自动修复三轮后仍未通过验证：\n{feedback}",
            test_file=test_file,
            attempts=attempts,
            diagnostic=diagnostic,
        )

    @staticmethod
    def _turn_prompt(task: str, test_file: str, attempt: int, feedback: str) -> str:
        base = (
            "你正在 Yuan Ye Agent 源码的隔离 Git worktree 中维护全局 Hook Extension。"
            "先阅读 extension/README.md，再完成用户需求。只能修改 extension/hook/**、"
            "extension/README.md 和 tests/extensions/**；禁止修改核心代码、.git、.yy 或凭据。"
            "每项能力使用描述性 Python 文件，不要把逻辑堆入初始空文件。"
            f"本轮必须创建测试文件 `{test_file}`，并主动运行它。"
            "完成后简要说明变更和测试结果。\n\n"
            f"用户需求：\n{task}"
        )
        if attempt > 1:
            base += (
                f"\n\n这是第 {attempt - 1} 轮自动修复。控制器上次验证失败：\n{feedback}\n"
                "请在同一 worktree 中修复这些问题，不要另建测试文件替代指定路径。"
            )
        return base

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
        return await HarnessEvolutionRunner._command(
            directory, command, check=check, timeout=timeout,
        )


async def _approve_coding_tool(name: str, arguments: dict[str, Any]) -> bool:
    """用户确认启动 Harness 后，允许隔离 worktree 内的既有 Coding 工具。"""
    del arguments
    return name != "skill_install"


def create_coding_runtime(
    config: RuntimeConfig,
    worktree_root: Path,
    *,
    sandbox: SandboxSessionProtocol | None = None,
) -> AgentRuntime:
    """复用正式 AgentRuntime 装配具备完整工作区能力的 Coding Agent。"""
    isolated = config.model_copy(update={
        "workspace_root": worktree_root.resolve(),
        "stream": False,
        "compression_threshold_tokens": config.compression_threshold_tokens or 20000,
    })
    skills = SkillService(
        isolated.agent_root,
        isolated.workspace_root,
        isolated.coding_source_root,
    )
    memory_root = isolated.agent_root / ".yy" / "harness-evolution" / "memory"
    long_term = HarnessLongTermMemory(
        memory_root / "profile",
        agent_root=isolated.agent_root,
    )
    long_term.ensure_project_initialized(isolated.workspace_root)
    memory = MemoryStore(
        memory_root,
        workspace_root=isolated.workspace_root,
        agent_root=isolated.agent_root,
        partition_by_workspace=False,
        profiles=long_term,
    )
    session_id = uuid4().hex[:16]
    memory.create_session("Harness Coding Agent 本次更新", session_id=session_id)
    web_search = (
        WebSearchTool(
            isolated.web_search_api_key,
            timeout_seconds=isolated.web_search_timeout_seconds,
            use_system_proxy=isolated.use_system_proxy,
            proxy_url=isolated.proxy_url,
        )
        if isolated.web_search_api_key
        else None
    )
    web_fetch = WebFetchTool(
        timeout_seconds=isolated.web_fetch_timeout_seconds,
        max_bytes=isolated.web_fetch_max_bytes,
        max_chars=isolated.web_fetch_max_chars,
        use_system_proxy=isolated.use_system_proxy,
        proxy_url=isolated.proxy_url,
    )
    # Harness 只暴露修复代码所需的最小能力。网页抓取始终可用；配置了
    # Brave Key 时额外加入搜索。论文、资料库、时间、计算、Cron 和 Skill
    # 安装仍不进入 Schema。
    selected_names = list(_CODING_BASE_TOOL_NAMES)
    if web_search is not None:
        selected_names.append("web_search")
    tools = default_tools(
        isolated.workspace_root,
        web_search_tool=web_search,
        web_fetch_tool=web_fetch,
    ).select(selected_names)
    tools.register(SkillReadTool(skills))
    register_subagent(tools, RuntimeSubagentRunner(isolated, tools))
    expected_tools = {*selected_names, "skill_read", "subagent"}
    if set(tools.names()) != expected_tools:
        raise RuntimeError("Harness Coding 工具目录偏离固定白名单")
    runtime = AgentRuntime(
        isolated,
        tools=tools,
        memory=memory,
        skills=skills,
        approval=_approve_coding_tool,
        enable_context_processing=True,
        enable_skills=True,
        enable_subagent=True,
        sandbox=sandbox,
        enable_sandbox=True,
        retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=2),
        raise_errors=True,
        enable_extensions=False,
        enable_cron=False,
        enable_references=False,
    )
    runtime.coding_session_id = session_id
    runtime.harness_long_term_memory = long_term
    return runtime


class HarnessEvolutionRunner:
    """管理 worktree，并为未来 Coding Tool/Skill 预留测试与合并路径。"""

    def __init__(
        self,
        writer: ErrorSnapshotWriter,
        *,
        runtime_factory: Callable[[RuntimeConfig, Path], AgentRuntime] = create_coding_runtime,
        memory_provider_factory: Callable[[RuntimeConfig], Any] | None = None,
    ) -> None:
        self.writer = writer
        self.runtime_factory = runtime_factory
        self.memory_provider_factory = memory_provider_factory

    async def run(self, request: HarnessEvolutionRequest) -> HarnessEvolutionResult:
        root = request.project_root.resolve()
        clean = await self._git(root, "status", "--porcelain", "--untracked-files=all")
        if clean.stdout.strip():
            message = "主 worktree 存在未提交修改，Harness 已停止且不会 stash 用户内容"
            self.writer.append_event(request.snapshot_path, "evolution", status="dirty_worktree", message=message)
            return HarnessEvolutionResult(status="dirty_worktree", message=message)
        branch_result = await self._git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            message = "当前不在可识别分支上，Harness 无法安全合并"
            self.writer.append_event(request.snapshot_path, "evolution", status="detached_head", message=message)
            return HarnessEvolutionResult(status="detached_head", message=message)
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
            try:
                runtime = self.runtime_factory(request.config, worktree)
            except Exception as exc:
                message = f"Coding Runtime 初始化失败：{str(exc) or type(exc).__name__}"
                self.writer.append_event(
                    request.snapshot_path,
                    "evolution",
                    status="coding_runtime_failed",
                    message=message,
                )
                return HarnessEvolutionResult(
                    status="coding_runtime_failed",
                    message=message,
                    worktree_path=str(worktree),
                    branch=branch,
                )
            if not _runtime_targets_worktree(runtime, worktree):
                await runtime.close()
                message = "Coding Runtime 的 workspace 未指向隔离 Git worktree，已拒绝运行"
                self.writer.append_event(
                    request.snapshot_path,
                    "evolution",
                    status="invalid_runtime_workspace",
                    message=message,
                    expected_workspace=str(worktree),
                )
                return HarnessEvolutionResult(
                    status="invalid_runtime_workspace",
                    message=message,
                    worktree_path=str(worktree),
                    branch=branch,
                )
            diagnostic_task = (
                "你是负责维护当前项目的 Coding Agent。当前 workspace 是隔离的 Git worktree。"
                "请结合专属项目记忆、已审核 Skill、文件搜索/读写工具、Subagent 和 Docker Bash，"
                "复现并修复下面的代码缺陷。只做解决问题所需的最小修改，不得修改 `.git`、`.yy`、"
                "凭据或本机配置；完成后说明修改和验证结果。Harness 会在你结束后统一执行完整测试，"
                "只有验证通过的变更才会合并。\n\n完整错误快照：\n"
                + request.snapshot_path.read_text(encoding="utf-8")
            )
            runtime_error: Exception | None = None
            try:
                coding_session_id = getattr(runtime, "coding_session_id", None)
                if coding_session_id:
                    result = await runtime.run(diagnostic_task, session_id=str(coding_session_id))
                else:
                    result = await runtime.run(diagnostic_task)
                diagnostic = result.answer
            except Exception as exc:
                diagnostic = f"诊断 Agent 失败：{str(exc) or type(exc).__name__}"
                runtime_error = exc
            finally:
                await runtime.close()
            self.writer.append_event(request.snapshot_path, "evolution", status="diagnosed", diagnostic=diagnostic)
            if runtime_error is not None:
                message = f"Coding Runtime 执行失败：{str(runtime_error) or type(runtime_error).__name__}"
                self.writer.append_event(
                    request.snapshot_path,
                    "evolution",
                    status="coding_runtime_failed",
                    message=message,
                )
                return HarnessEvolutionResult(
                    status="coding_runtime_failed",
                    message=message,
                    worktree_path=str(worktree),
                    branch=branch,
                )
            changes = (await self._git(worktree, "status", "--porcelain", "--untracked-files=all")).stdout
            if not changes.strip():
                message = "Coding Agent 完成诊断，但未产生代码变更"
                self.writer.append_event(
                    request.snapshot_path,
                    "evolution",
                    status="no_code_changes",
                    message=message,
                    worktree_path=str(worktree),
                )
                return HarnessEvolutionResult(
                    status="no_code_changes", message=message, worktree_path=str(worktree), branch=branch,
                )

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
                return HarnessEvolutionResult(
                    status="forbidden_changes", message=message, worktree_path=str(worktree), branch=branch,
                )

            self.writer.append_event(request.snapshot_path, "evolution", status="changes_detected", git_status=changes)
            await self._git(worktree, "add", "--intent-to-add", "--all")
            tests = await self._run_tests(worktree, request.snapshot_path)
            if not tests:
                return HarnessEvolutionResult(
                    status="tests_failed", message="新版本测试失败，已丢弃隔离 worktree",
                    worktree_path=str(worktree), branch=branch,
                )
            await self._git(worktree, "add", "--all")
            await self._git(
                worktree,
                "-c", "user.name=Yuan Ye Harness",
                "-c", "user.email=harness@local.invalid",
                "commit", "-m", f"Harness evolution {request.incident_id[:12]}",
            )
            repair_commit = (await self._git(worktree, "rev-parse", "HEAD")).stdout.strip()
            changed_files = [
                line.strip()
                for line in (
                    await self._git(worktree, "diff", "--name-only", f"{base}..{repair_commit}")
                ).stdout.splitlines()
                if line.strip()
            ]
            if (await self._git(root, "status", "--porcelain", "--untracked-files=all")).stdout.strip():
                keep_branch = True
                message = "验证后主 worktree 发生变化，已拒绝自动合并并保留临时分支"
                self.writer.append_event(request.snapshot_path, "evolution", status="main_changed", message=message, branch=branch)
                return HarnessEvolutionResult(
                    status="main_changed", message=message, worktree_path=str(worktree), branch=branch,
                )
            if (await self._git(root, "rev-parse", "HEAD")).stdout.strip() != base:
                keep_branch = True
                message = "验证期间主分支 HEAD 已变化，已拒绝自动合并并保留临时分支"
                self.writer.append_event(request.snapshot_path, "evolution", status="main_changed", message=message, branch=branch)
                return HarnessEvolutionResult(
                    status="main_changed", message=message, worktree_path=str(worktree), branch=branch,
                )
            await self._git(root, "merge", "--ff-only", branch)
            self.writer.append_event(request.snapshot_path, "evolution", status="merged", branch=branch)
            try:
                await self._update_long_term_memory(
                    request,
                    root,
                    diagnostic=diagnostic,
                    commit_sha=repair_commit,
                    changed_files=changed_files,
                )
            except Exception as exc:
                try:
                    self.writer.append_event(
                        request.snapshot_path,
                        "evolution",
                        status="long_term_memory_failed",
                        message=str(exc) or type(exc).__name__,
                    )
                except Exception:
                    pass
            return HarnessEvolutionResult(
                status="merged", message="修复已合并，下次启动生效",
                worktree_path=str(worktree), branch=branch, merged=True,
            )
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

    async def _update_long_term_memory(
        self,
        request: HarnessEvolutionRequest,
        root: Path,
        *,
        diagnostic: str,
        commit_sha: str,
        changed_files: list[str],
    ) -> None:
        """仅在成功合并后维护四文件长期记忆，失败时使用确定性降级。"""
        long_term = HarnessLongTermMemory(
            request.config.agent_root / ".yy" / "harness-evolution" / "memory" / "profile",
            agent_root=request.config.agent_root,
        )
        long_term.ensure_project_initialized(root)
        mode = "model"
        error = ""
        try:
            update = await self._curate_long_term_memory(
                request,
                root,
                long_term,
                diagnostic=diagnostic,
                commit_sha=commit_sha,
                changed_files=changed_files,
            )
        except Exception as exc:
            mode = "deterministic_fallback"
            error = str(exc) or type(exc).__name__
            update = long_term.deterministic_update(
                root,
                task=request.task,
                commit_sha=commit_sha,
                changed_files=changed_files,
            )
        try:
            await long_term.apply_update(update)
            self.writer.append_event(
                request.snapshot_path,
                "evolution",
                status="long_term_memory_updated",
                mode=mode,
                files=["PROJECT.md", "CHANGES.md"] + (
                    ["LESSONS.md"] if update.lesson_entry_markdown else []
                ),
                curator_error=error,
            )
        except Exception as exc:
            self.writer.append_event(
                request.snapshot_path,
                "evolution",
                status="long_term_memory_failed",
                mode=mode,
                message=str(exc) or type(exc).__name__,
                curator_error=error,
            )

    async def _curate_long_term_memory(
        self,
        request: HarnessEvolutionRequest,
        root: Path,
        long_term: HarnessLongTermMemory,
        *,
        diagnostic: str,
        commit_sha: str,
        changed_files: list[str],
    ) -> HarnessMemoryUpdate:
        """通过无工具、无持久化维护 Runtime 生成长期记忆更新。"""
        if self.memory_provider_factory is None and not request.config.api_key:
            raise RuntimeError("未配置维护模型凭据")
        payload = {
            "updated_at": _timestamp(),
            "task": request.task,
            "diagnostic": diagnostic,
            "commit_sha": commit_sha,
            "changed_files": changed_files,
            "tests": "Harness 固定测试集全部通过",
            "existing_long_term_memory": long_term.load_for_session(None),
            "deterministic_project_snapshot": long_term.deterministic_update(
                root,
                task=request.task,
                commit_sha=commit_sha,
                changed_files=changed_files,
            ).project_markdown,
        }
        messages = compose_harness_memory_messages(payload)
        hooks = HookRegistry()

        async def inject_prompt(event: HookEvent) -> None:
            event.data["messages"] = [dict(message) for message in messages]
            event.data["tools"] = []

        hooks.register(HookPoint.MODEL_BEFORE, inject_prompt, priority=-100)
        curator_config = request.config.model_copy(update={
            "workspace_root": root.resolve(),
            "stream": False,
            "compression_threshold_tokens": 0,
        })
        provider = (
            self.memory_provider_factory(curator_config)
            if self.memory_provider_factory is not None
            else build_provider(
                curator_config.provider,
                curator_config.model,
                base_url=curator_config.base_url,
                api_key=curator_config.api_key,
                stream=False,
                use_system_proxy=curator_config.use_system_proxy,
                proxy_url=curator_config.proxy_url,
            )
        )
        runtime = AgentRuntime(
            curator_config,
            provider=provider,
            tools=AsyncToolRegistry(),
            hooks=hooks,
            enable_context_processing=False,
            enable_skills=False,
            enable_subagent=False,
            enable_sandbox=False,
            raise_errors=True,
            enable_extensions=False,
            enable_references=False,
        )
        result = await runtime.run("维护 Harness Coding Agent 长期记忆")
        if not result.completed:
            raise RuntimeError("长期记忆维护 Runtime 未返回完整结果")
        return _parse_harness_memory_update(result.answer)

    async def _run_tests(self, worktree: Path, snapshot_path: Path) -> bool:
        commands = [
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "Agent", "bootstrap", "context_process", "dream", "extension", "gateway", "memory", "prompt", "reference", "sandbox", "skill", "tools", "run_ui", "tests", "harness-evolution", "run.py"],
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
        branch_result = _CommandResult(returncode=0, stdout="", stderr="")
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
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"命令执行失败：{' '.join(command)}\n{result.stderr or result.stdout}")
        return result


class _CommandResult(BaseModel):
    """隔离命令执行结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    returncode: int = Field(ge=0)
    stdout: str
    stderr: str


def _runtime_targets_worktree(runtime: Any, worktree: Path) -> bool:
    """要求 Coding Runtime 暴露的全部工作区边界都严格指向隔离 worktree。"""
    roots: list[Path] = []
    configured = getattr(getattr(runtime, "config", None), "workspace_root", None)
    if configured is not None:
        roots.append(Path(configured).resolve())
    tool_root = getattr(getattr(runtime, "tool_context", None), "project_root", None)
    if tool_root is not None:
        roots.append(Path(tool_root).resolve())
    declared = getattr(runtime, "workspace_root", None)
    if declared is not None:
        roots.append(Path(declared).resolve())
    expected = worktree.resolve()
    return bool(roots) and all(root == expected for root in roots)


def _parse_harness_memory_update(raw: str) -> HarnessMemoryUpdate:
    """兼容常见 JSON 代码围栏，并交由 Pydantic 严格校验。"""
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
        if value.startswith("json"):
            value = value[4:].lstrip()
    return HarnessMemoryUpdate.model_validate_json(value)


def _session_audit(record: dict[str, Any], position: int) -> dict[str, Any]:
    """只保留 Session 审计字段，避免再次复制实际模型消息内容。"""
    message_fields = {"content", "tool_calls"}
    return {
        "record_type": "session_audit",
        "session_position": position,
        **{key: value for key, value in record.items() if key not in message_fields},
    }


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


def _code_slug(task: str) -> str:
    """从需求中生成稳定、合法且较短的测试名称前缀。"""
    ascii_words = re.findall(r"[a-z0-9]+", task.casefold())
    if ascii_words:
        return "_".join(ascii_words[:4])[:32].strip("_") or "extension"
    return "extension"


def _status_paths(status: str) -> list[str]:
    """提取 Git porcelain v1 的目标路径，供严格路径白名单使用。"""
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip().strip('"')
        if " -> " in value:
            value = value.split(" -> ", 1)[1].strip().strip('"')
        paths.append(value.replace("\\", "/"))
    return paths


def _validate_changed_extensions(worktree: Path, changed: list[str]) -> str:
    """加载整个不可变目录快照，并补充本轮文件位置约束。"""
    for relative in changed:
        path = worktree / relative
        if path.exists():
            parts = Path(relative).parts
            if len(parts) != 4 or parts[:2] != ("extension", "hook") or path.suffix != ".py":
                return f"Extension Python 文件必须是阶段目录的直接子文件：{relative}"
    try:
        ExtensionLoader(worktree).scan()
    except Exception as exc:
        return f"Extension 契约校验失败：{str(exc) or type(exc).__name__}"
    return ""
