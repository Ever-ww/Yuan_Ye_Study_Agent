"""Harness 自进化的错误快照、隔离 worktree 与可扩展验证流水线。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from backup.security import SensitiveEnvSanitizer

from Agent import AgentRuntime, EventType, ExtensionLoader, ModelRetryPolicy, RuntimeConfig, RuntimeFailure
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from memory import HarnessLongTermMemory, HarnessMemoryUpdate, MemoryStore
from prompt import compose_harness_memory_messages
from sandbox import SandboxSessionProtocol
from tool import (
    AsyncToolRegistry,
    register_subagent,
)
from gateway.harness_dream import DreamEvolutionContext
from harness_runtime import (
    HarnessDynamicContextController,
    HarnessPromptComposer,
    HarnessPromptPrefixCache,
    HarnessRuntimeProfile,
    HarnessRuntimeResourceLoader,
    HarnessRuntimeTrigger,
    HarnessTraceContext,
    register_harness_context_callbacks,
)


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


class HarnessOriginContext(BaseModel):
    """Immutable snapshot that relates a Harness invocation to its parent Gateway work."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    origin_project_id: str
    origin_session_id: str | None = None
    origin_run_id: str
    session_record_ids: tuple[str, ...] = ()
    session_records_hash: str = ""
    context_summary: str = ""
    trigger_evidence: dict[str, Any] = Field(default_factory=dict)


class CapabilityGap(BaseModel):
    """A behaviour-level description of a missing Yuan Ye Tool capability."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    summary: str = Field(min_length=1)
    desired_behavior: str = Field(min_length=1)
    current_limitation: str = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    safety_constraints: tuple[str, ...] = ()


class HarnessRepairPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    max_attempts: int = Field(default=4, ge=1, le=4)
    allow_automatic_repair: bool = True


class HarnessEvolutionRequest(BaseModel):
    """一次经用户确认的隔离诊断请求。"""

    model_config = ConfigDict(frozen=True, strict=True)

    task: str
    config: RuntimeConfig
    # Legacy ERROR fields remain optional while all three triggers share this envelope.
    project_root: Path | None = None
    incident_id: str | None = None
    snapshot_path: Path | None = None
    trigger: Literal["manual", "error", "capability", "dream"] = "error"
    target: Literal["extension", "tool", "source_repair", "dream_optimize"] = "source_repair"
    source_root: Path | None = None
    agent_root: Path | None = None
    invocation_id: str | None = None
    operation_id: str | None = None
    capability_gap: CapabilityGap | None = None
    dream_context: DreamEvolutionContext | None = None
    origin: HarnessOriginContext | None = None
    max_attempts: int = Field(default=1, ge=1, le=4)
    merge_policy: Literal["immediate", "deferred"] = "immediate"

    @property
    def repair_policy(self) -> HarnessRepairPolicy:
        return HarnessRepairPolicy(
            max_attempts=self.max_attempts,
            allow_automatic_repair=self.max_attempts > 1,
        )

    def resolved_source_root(self) -> Path:
        return (self.source_root or self.project_root or self.config.coding_source_root or self.config.workspace_root).resolve()

    def resolved_agent_root(self) -> Path:
        return (self.agent_root or self.config.agent_root).resolve()

    def resolved_invocation_id(self) -> str:
        if self.invocation_id:
            return self.invocation_id
        if self.operation_id:
            return hashlib.sha256(f"capability:{self.operation_id}".encode("utf-8")).hexdigest()[:32]
        if self.incident_id:
            return hashlib.sha256(f"error:{self.incident_id}".encode("utf-8")).hexdigest()[:32]
        return uuid4().hex


class HarnessEvolutionResult(BaseModel):
    """Harness 流水线的最终状态。"""

    model_config = ConfigDict(frozen=True, strict=True)

    status: str
    message: str
    worktree_path: str = ""
    branch: str = ""
    merged: bool = False
    invocation_id: str = ""
    verified_commit: str = ""
    merged_commit: str = ""
    changed_files: tuple[str, ...] = ()
    tests_summary: str = ""
    restart_required: bool = False


class HarnessEvolutionInvocation(BaseModel):
    """Immutable identity and final evidence summary for one Engine execution."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    invocation_id: str
    trigger: Literal["manual", "error", "capability", "dream"]
    target: Literal["extension", "tool", "source_repair", "dream_optimize"]
    source_identity: str
    origin: HarnessOriginContext | None = None
    base_commit: str
    verified_commit: str = ""
    merged_commit: str = ""
    status: str


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
    origin: HarnessOriginContext | None = None


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

    async def start(
        self, source_root: Path | None = None, *, origin: HarnessOriginContext | None = None,
    ) -> CodeSessionRecord:
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
            runtime = _create_profiled_runtime(
                factory,
                self.config,
                worktree,
                trigger="manual",
                target="extension",
                invocation_id=code_id,
            )
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
                origin=origin.model_dump(mode="json") if origin else None,
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
                origin=origin,
            )
            return self.record
        except Exception:
            await self._git(root, "worktree", "remove", "--force", str(worktree), check=False)
            await self._git(root, "branch", "-D", branch, check=False)
            raise

    async def _run_turn_legacy(self, task: str) -> CodeTurnResult:
        """Compatibility alias; the single Evolution Engine owns the execution pipeline."""
        return await self.run_turn(task)

    async def run_turn(self, task: str) -> CodeTurnResult:
        """Thin MANUAL adapter: the shared engine owns generation, repair and validation."""
        return await HarnessEvolutionEngine.for_config(
            self.config,
            runtime_factory=self.runtime_factory or create_coding_runtime,
            memory_provider_factory=self.memory_provider_factory,
        ).run_manual_turn(self, task)

    async def finalize(self) -> CodeFinalizeResult:
        return await HarnessEvolutionEngine.for_config(
            self.config,
            runtime_factory=self.runtime_factory or create_coding_runtime,
            memory_provider_factory=self.memory_provider_factory,
        ).finalize_manual(self)

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
    profile: HarnessRuntimeProfile | None = None,
    trace_context: HarnessTraceContext | None = None,
    prefix_cache: HarnessPromptPrefixCache | None = None,
) -> AgentRuntime:
    """复用正式 AgentRuntime 装配具备完整工作区能力的 Coding Agent。"""
    isolated = config.model_copy(update={
        "workspace_root": worktree_root.resolve(),
        "stream": False,
        "compression_threshold_tokens": config.compression_threshold_tokens or 200000,
    })
    resource_loader = HarnessRuntimeResourceLoader(Path(__file__).resolve().parent / "runtime")
    selected_profile = profile or resource_loader.profile(HarnessRuntimeTrigger.MANUAL)
    skills = resource_loader.build_skills(
        selected_profile,
        agent_root=isolated.agent_root,
        workspace_root=isolated.workspace_root,
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
    tools = resource_loader.build_tools(selected_profile, isolated, skills)
    tool_catalog_hash = resource_loader.tool_catalog_hash(tools)
    prompts = HarnessPromptComposer(
        selected_profile,
        skills,
        tool_catalog_hash=tool_catalog_hash,
        prefix_cache=prefix_cache,
    )
    selected_trace = trace_context or HarnessTraceContext(
        trace_id=uuid4().hex,
        trigger=selected_profile.trigger,
        target="extension" if selected_profile.trigger is HarnessRuntimeTrigger.MANUAL else "source_repair",
        invocation_id=uuid4().hex,
        prompt_profile_hash=prompts.prompt_profile.cache_key,
        tool_catalog_hash=tool_catalog_hash,
        skill_catalog_hash=skills.catalog_snapshot().digest,
        context_epoch=1,
        created_at=datetime.now().astimezone(),
    )
    if selected_trace.trigger is not selected_profile.trigger:
        raise ValueError("Harness Trace trigger does not match its isolated Runtime profile")
    if selected_trace.tool_catalog_hash != tool_catalog_hash:
        raise ValueError("Harness Trace Tool catalog hash does not match the loaded profile")
    if selected_trace.skill_catalog_hash != skills.catalog_snapshot().digest:
        raise ValueError("Harness Trace Skill catalog hash does not match the loaded profile")
    if selected_trace.prompt_profile_hash != prompts.prompt_profile.cache_key:
        raise ValueError("Harness Trace prompt profile hash does not match the loaded profile")
    dynamic_context = HarnessDynamicContextController(selected_trace)
    runtime = AgentRuntime(
        isolated,
        tools=tools,
        memory=memory,
        skills=skills,
        prompt_composer=prompts,
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
        runtime_profile="harness",
    )
    register_harness_context_callbacks(runtime.hooks, dynamic_context)
    runtime.coding_session_id = session_id
    runtime.harness_long_term_memory = long_term
    runtime.harness_runtime_profile = selected_profile
    runtime.harness_trace_context = selected_trace
    runtime.harness_dynamic_context = dynamic_context
    runtime.harness_prompt_profile = prompts.prompt_profile
    return runtime


def _runtime_profile_and_trace(
    config: RuntimeConfig,
    worktree: Path,
    *,
    trigger: str,
    target: str,
    invocation_id: str,
) -> tuple[HarnessRuntimeProfile, HarnessTraceContext]:
    resource_loader = HarnessRuntimeResourceLoader(Path(__file__).resolve().parent / "runtime")
    profile = resource_loader.profile(HarnessRuntimeTrigger(trigger))
    isolated = config.model_copy(update={"workspace_root": worktree.resolve(), "stream": False})
    skills = resource_loader.build_skills(
        profile,
        agent_root=isolated.agent_root,
        workspace_root=isolated.workspace_root,
    )
    tools = resource_loader.build_tools(profile, isolated, skills)
    tool_hash = resource_loader.tool_catalog_hash(tools)
    prompts = HarnessPromptComposer(profile, skills, tool_catalog_hash=tool_hash)
    trace = HarnessTraceContext(
        trace_id=uuid4().hex,
        trigger=profile.trigger,
        target=target,
        invocation_id=invocation_id,
        prompt_profile_hash=prompts.prompt_profile.cache_key,
        tool_catalog_hash=tool_hash,
        skill_catalog_hash=skills.catalog_snapshot().digest,
        context_epoch=1,
        created_at=datetime.now().astimezone(),
    )
    return profile, trace


def _create_profiled_runtime(
    factory: Callable[..., AgentRuntime],
    config: RuntimeConfig,
    worktree: Path,
    *,
    trigger: str,
    target: str,
    invocation_id: str,
) -> AgentRuntime:
    signature = inspect.signature(factory)
    supports_profile = (
        "profile" in signature.parameters
        and "trace_context" in signature.parameters
    ) or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if not supports_profile:
        return factory(config, worktree)
    profile, trace = _runtime_profile_and_trace(
        config,
        worktree,
        trigger=trigger,
        target=target,
        invocation_id=invocation_id,
    )
    return factory(
        config,
        worktree,
        profile=profile,
        trace_context=trace,
    )


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

    async def _run_legacy(self, request: HarnessEvolutionRequest) -> HarnessEvolutionResult:
        """Compatibility alias; ERROR execution is owned by HarnessEvolutionEngine."""
        return await HarnessEvolutionEngine.for_config(
            request.config,
            runtime_factory=self.runtime_factory,
            memory_provider_factory=self.memory_provider_factory,
        ).run_error(self, request)

    async def run(self, request: HarnessEvolutionRequest) -> HarnessEvolutionResult:
        """ERROR facade retained for compatibility; execution is dispatched by the engine."""
        return await HarnessEvolutionEngine.for_config(
            request.config,
            runtime_factory=self.runtime_factory,
            memory_provider_factory=self.memory_provider_factory,
        ).run_error(self, request)

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
            env=SensitiveEnvSanitizer.subprocess_env(),
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


class HarnessInvocationAudit:
    """Append-only invocation evidence kept under Agent Home, never inside the source repo."""

    def __init__(self, agent_root: Path) -> None:
        self.root = (agent_root / ".yy" / "harness-evolution" / "invocations").resolve()

    def create(self, source_root: Path, invocation_id: str, **record: Any) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
            raise ValueError("Harness invocation_id must be a 32-character lowercase hex value")
        identity = hashlib.sha256(str(source_root.resolve()).casefold().encode("utf-8")).hexdigest()[:16]
        directory = self.root / identity
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{invocation_id}.jsonl"
        created = not path.exists()
        if created:
            path.touch()
            self.append(
                path, "invocation_started", invocation_id=invocation_id,
                source_identity=identity, **record,
            )
        return path

    def append(self, path: Path, event: str, **record: Any) -> None:
        try:
            sequence = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
        except OSError:
            sequence = 1
        occurred_at = datetime.now().astimezone().isoformat(timespec="seconds")
        event_identity = hashlib.sha256(
            json.dumps(
                {"path": path.name, "sequence": sequence, "event": event, "record": _sanitize(record)},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        metadata: dict[str, Any] = {
            "event": event, "timestamp": _timestamp(), "occurred_at": occurred_at,
            "sequence": sequence, "audit_event_id": event_identity,
        }
        if event == "merge_committed":
            metadata["merge_event_id"] = event_identity
        payload = json.dumps(
            _sanitize({**metadata, **record}),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            try:
                import os
                os.fsync(handle.fileno())
            except OSError:
                pass


class HarnessEvolutionEngine:
    """The common Harness evolution dispatch point for all four Harness triggers.

    The legacy facades intentionally remain thin: their trigger evidence and outer lifecycle
    differ, but all new capability evolution and all facade calls enter here first.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        runtime_factory: Callable[[RuntimeConfig, Path], AgentRuntime] = create_coding_runtime,
        memory_provider_factory: Callable[[RuntimeConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self.runtime_factory = runtime_factory
        self.memory_provider_factory = memory_provider_factory
        self.audit = HarnessInvocationAudit(config.agent_root)
        self._tool_registry_baselines: dict[str, dict[str, Any]] = {}

    @classmethod
    def for_config(cls, config: RuntimeConfig, **kwargs: Any) -> "HarnessEvolutionEngine":
        return cls(config, **kwargs)

    async def run_error(
        self, facade: HarnessEvolutionRunner, request: HarnessEvolutionRequest,
    ) -> HarnessEvolutionResult:
        if request.snapshot_path is None or not request.snapshot_path.is_file():
            raise ValueError("ERROR evolution requires a durable ErrorSnapshot")
        selected = request.model_copy(update={
            "trigger": "error",
            "target": "source_repair",
            "max_attempts": 4,
            "merge_policy": "immediate",
        })
        injected_validator = (
            facade._run_tests
            if type(facade)._run_tests is not HarnessEvolutionRunner._run_tests
            else None
        )
        return await self._run_isolated(
            selected, error_writer=facade.writer, error_validator=injected_validator,
            error_facade=facade,
        )

    async def run_manual_turn(self, controller: CodeSessionController, task: str) -> CodeTurnResult:
        record, runtime = controller._active()
        requirement = task.strip()
        if not requirement:
            raise ValueError("Coding 需求不能为空")
        test_id = hashlib.sha256(
            f"{record.code_session_id}:{record.verified_turns}:{requirement}".encode("utf-8")
        ).hexdigest()[:8]
        test_file = f"tests/extensions/test_{_code_slug(requirement)}_{test_id}.py"
        controller._requirements.append(requirement)
        controller.audit.append_event(
            record.audit_path, "code_turn_started", task=requirement,
            required_test_file=test_file, trigger="manual", target="extension",
        )
        diagnostic, feedback, attempts = await self._repair_loop(
            runtime=runtime,
            request=HarnessEvolutionRequest(
                task=requirement, config=self.config, trigger="manual", target="extension",
                source_root=record.source_root, agent_root=self.config.agent_root,
                invocation_id=hashlib.sha256(
                    f"manual:{record.code_session_id}:{record.verified_turns + 1}".encode("utf-8")
                ).hexdigest()[:32],
                origin=record.origin,
                max_attempts=4, merge_policy="deferred",
            ),
            worktree=record.worktree_path,
            test_file=test_file,
            audit_path=record.audit_path,
            audit=lambda event, **data: controller.audit.append_event(record.audit_path, event, **data),
            compatibility_validator=(
                controller._validate_and_test
                if type(controller)._validate_and_test is not CodeSessionController._validate_and_test
                else None
            ),
            manual_record=record,
        )
        if feedback:
            controller.record = record.model_copy(update={"status": "unverified"})
            return controller._failed_turn(record, test_file, attempts, feedback, diagnostic)
        await self._commit_candidate(
            record.worktree_path,
            f"Extension: {_code_slug(requirement)} ({record.verified_turns + 1})",
        )
        commit = (await self._command(record.worktree_path, ["git", "rev-parse", "HEAD"])).stdout.strip()
        controller.record = record.model_copy(update={
            "last_verified_commit": commit,
            "verified_turns": record.verified_turns + 1,
            "status": "active",
        })
        controller.audit.append_event(
            record.audit_path, "code_turn_verified", attempt=attempts,
            commit=commit, test_file=test_file, diagnostic=diagnostic,
        )
        return CodeTurnResult(
            code_session_id=record.code_session_id, status="verified",
            message="扩展代码和测试已通过统一 Harness Engine 验证，并提交到隔离临时分支。",
            test_file=test_file, attempts=attempts, commit=commit, diagnostic=diagnostic,
        )

    async def finalize_manual(self, controller: CodeSessionController) -> CodeFinalizeResult:
        record, runtime = controller._active()
        status = (await self._command(
            record.worktree_path, ["git", "status", "--porcelain", "--untracked-files=all"],
        )).stdout.strip()
        head = (await self._command(record.worktree_path, ["git", "rev-parse", "HEAD"])).stdout.strip()
        if status or head != record.last_verified_commit or record.status == "unverified":
            return CodeFinalizeResult(
                code_session_id=record.code_session_id, status="unverified_changes",
                message="worktree 存在未验证修改，不能合并；请继续修复后再输入 /exit。",
                stay_in_code_mode=True, worktree_path=str(record.worktree_path), branch=record.branch,
            )
        await runtime.close()
        controller.runtime = None
        if record.last_verified_commit == record.base_commit:
            await controller._cleanup(record, keep_branch=False)
            controller.record = None
            return CodeFinalizeResult(
                code_session_id=record.code_session_id, status="no_changes",
                message="本次 Coding Session 没有已验证改动，已清理并返回普通聊天。",
            )
        merge = await self._merge_verified(
            root=record.source_root, base=record.base_commit,
            verified=record.last_verified_commit, branch=record.branch,
            audit=lambda event, **data: controller.audit.append_event(record.audit_path, event, **data),
            invocation_id=record.code_session_id, trigger="manual",
        )
        if merge != "merged":
            controller.record = record.model_copy(update={"status": "main_changed"})
            return CodeFinalizeResult(
                code_session_id=record.code_session_id, status="main_changed",
                message="源码仓库 HEAD 已变化，已保留临时分支与 worktree，未执行合并。",
                worktree_path=str(record.worktree_path), branch=record.branch,
            )
        changed = await self._changed_files(record.source_root, record.base_commit, record.last_verified_commit)
        await controller._update_long_term(record, changed)
        await controller._cleanup(record, keep_branch=False)
        controller.record = None
        return CodeFinalizeResult(
            code_session_id=record.code_session_id, status="merged",
            message="已由统一 Harness Engine fast-forward 合并验证通过的 Hook。",
            merged=True,
        )

    async def run(self, request: HarnessEvolutionRequest) -> HarnessEvolutionResult:
        if request.trigger == "error":
            if request.snapshot_path is None:
                raise ValueError("ERROR evolution must reference a real ErrorSnapshot")
            writer = ErrorSnapshotWriter(request.resolved_agent_root())
            return await self._run_isolated(request, error_writer=writer)
        if request.trigger == "manual":
            raise ValueError("MANUAL evolution must use CodeSessionController")
        return await self._run_isolated(request)

    async def _run_isolated(
        self,
        request: HarnessEvolutionRequest,
        *,
        error_writer: ErrorSnapshotWriter | None = None,
        error_validator: Callable[[Path, Path], Any] | None = None,
        error_facade: HarnessEvolutionRunner | None = None,
    ) -> HarnessEvolutionResult:
        if request.trigger == "capability" and request.target != "tool":
            return HarnessEvolutionResult(
                status="requires_broader_source_change",
                message="CAPABILITY is restricted to Tool evolution",
            )
        if request.trigger == "dream" and (
            request.target != "dream_optimize" or request.dream_context is None
        ):
            return HarnessEvolutionResult(
                status="confirmed_failed",
                message="DREAM evolution requires an immutable DREAM_OPTIMIZE changeset",
            )
        root = request.resolved_source_root()
        invocation_id = request.resolved_invocation_id()
        test_file = (
            f"tests/tools/test_{_code_slug(request.task)}_"
            f"{hashlib.sha256(invocation_id.encode('utf-8')).hexdigest()[:8]}.py"
            if request.target == "tool" else ""
        )
        if request.target == "dream_optimize":
            test_file = (
                f"tests/harness_dream/test_{request.dream_context.changeset.date.replace('-', '_')}_"
                f"{hashlib.sha256(invocation_id.encode('utf-8')).hexdigest()[:8]}.py"
            )
        source_identity = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:16]
        if request.target == "tool":
            self._tool_registry_baselines[invocation_id] = await self._registry_snapshot(root)
        audit_path = self.audit.create(
            root, invocation_id, trigger=request.trigger, target=request.target,
            operation_id=request.operation_id, task=request.task,
            capability_gap=request.capability_gap.model_dump(mode="json") if request.capability_gap else None,
            origin=request.origin.model_dump(mode="json") if request.origin else None,
        )
        def record_event(event: str, **data: Any) -> None:
            self.audit.append(audit_path, event, **data)
            if error_writer is not None and request.snapshot_path is not None:
                status = str(data.get("status") or event)
                error_writer.append_event(
                    request.snapshot_path, "evolution", status=status,
                    event=event, **{key: value for key, value in data.items() if key != "status"},
                )
        clean = await HarnessEvolutionRunner._command(root, ["git", "status", "--porcelain", "--untracked-files=all"], check=False)
        if clean.returncode != 0 or clean.stdout.strip():
            record_event("finished", status="confirmed_failed", reason="source_not_clean")
            status = (
                "dirty_worktree" if request.trigger == "error"
                else "deferred" if request.trigger == "dream"
                else "confirmed_failed"
            )
            return HarnessEvolutionResult(status=status, message="Source repository must be clean before Harness evolution", invocation_id=invocation_id)
        branch_result = await HarnessEvolutionRunner._command(root, ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            return HarnessEvolutionResult(status="confirmed_failed", message="Detached HEAD cannot safely evolve Harness")
        base = (await HarnessEvolutionRunner._command(root, ["git", "rev-parse", "HEAD"])).stdout.strip()
        branch = f"harness-{request.trigger}/{invocation_id[:16]}"
        worktree = request.resolved_agent_root() / ".yy" / "harness-evolution" / "worktrees" / source_identity / invocation_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        added = await HarnessEvolutionRunner._command(root, ["git", "worktree", "add", "-b", branch, str(worktree), base], check=False)
        if added.returncode != 0:
            record_event("finished", status="unknown", reason=added.stderr[-2048:])
            return HarnessEvolutionResult(status="unknown", message="Could not create isolated Harness worktree")
        keep = True
        try:
            record_event("worktree_created", base_commit=base, branch=branch, worktree=str(worktree))
            runtime = _create_profiled_runtime(
                self.runtime_factory,
                request.config,
                worktree,
                trigger=request.trigger,
                target=request.target,
                invocation_id=invocation_id,
            )
            if not _runtime_targets_worktree(runtime, worktree):
                await runtime.close()
                record_event("finished", status="invalid_runtime_workspace")
                keep = False
                return HarnessEvolutionResult(
                    status="invalid_runtime_workspace",
                    message="Coding runtime escaped its isolated worktree",
                    worktree_path=str(worktree), branch=branch,
                    invocation_id=invocation_id,
                )
            diagnostic, feedback, attempts = await self._repair_loop(
                runtime=runtime, request=request, worktree=worktree,
                test_file=test_file, audit_path=audit_path,
                audit=record_event,
                error_writer=error_writer,
                error_validator=error_validator,
            )
            await runtime.close()
            if feedback == "Coding Agent produced no source changes":
                keep = False
                record_event("finished", status="no_code_changes")
                return HarnessEvolutionResult(
                    status="no_code_changes",
                    message="Coding Agent completed without source changes",
                    worktree_path=str(worktree), branch=branch,
                    invocation_id=invocation_id,
                )
            if feedback:
                if feedback.startswith("requires broader source change:"):
                    status = "requires_broader_source_change"
                elif request.trigger == "error" and feedback.startswith("Coding Runtime failed:"):
                    status = "coding_runtime_failed"
                    keep = False
                else:
                    status = "tests_failed" if request.trigger == "error" else "confirmed_failed"
                if request.trigger == "error":
                    # A deterministic validation failure has no unknown Git side effect;
                    # export is already in the audit/ErrorSnapshot and the candidate is safe to clean.
                    keep = False
                record_event("finished", status=status, diagnostic=diagnostic, feedback=feedback)
                return HarnessEvolutionResult(
                    status=status, message=f"Harness evolution did not validate: {feedback}",
                    worktree_path=str(worktree), branch=branch, invocation_id=invocation_id,
                    tests_summary=feedback,
                )
            changes = (await self._command(worktree, ["git", "status", "--porcelain", "--untracked-files=all"])).stdout
            if not changes.strip():
                keep = False
                return HarnessEvolutionResult(
                    status="no_code_changes", message="Coding Agent completed without source changes",
                    worktree_path=str(worktree), branch=branch, invocation_id=invocation_id,
                )
            await self._commit_candidate(worktree, f"Harness {request.trigger} {invocation_id[:12]}")
            verified = (await HarnessEvolutionRunner._command(worktree, ["git", "rev-parse", "HEAD"])).stdout.strip()
            merge_status = await self._merge_verified(
                root=root, base=base, verified=verified, branch=branch,
                audit=record_event,
                invocation_id=invocation_id, trigger=request.trigger,
                target_branch=branch_result.stdout.strip(),
            )
            if merge_status != "merged":
                return HarnessEvolutionResult(
                    status=merge_status, message="Main branch changed or merge result is unknown; candidate preserved",
                    worktree_path=str(worktree), branch=branch, invocation_id=invocation_id,
                    verified_commit=verified,
                )
            keep = False
            changed_files = await self._changed_files(root, base, verified)
            if error_facade is not None:
                await error_facade._update_long_term_memory(
                    request, root, diagnostic=diagnostic,
                    commit_sha=verified, changed_files=changed_files,
                )
            elif request.trigger in {"capability", "dream"}:
                await self._update_shared_memory(
                    root, request.task, verified, changed_files, record_event,
                )
            return HarnessEvolutionResult(
                status="merged", message="Harness evolution merged safely",
                worktree_path=str(worktree), branch=branch, merged=True,
                invocation_id=invocation_id, verified_commit=verified,
                merged_commit=verified, changed_files=tuple(changed_files),
                tests_summary=f"validated in {attempts} attempt(s)",
                restart_required=request.trigger in {"capability", "dream"},
            )
        finally:
            if not keep:
                await HarnessEvolutionRunner._command(root, ["git", "worktree", "remove", "--force", str(worktree)], check=False)
                await HarnessEvolutionRunner._command(root, ["git", "branch", "-D", branch], check=False)
                record_event(
                    "cleanup", status="cleanup", former_worktree_path=str(worktree),
                    branch=branch, branch_preserved=False,
                )

    async def _repair_loop(
        self, *, runtime: Any, request: HarnessEvolutionRequest, worktree: Path,
        test_file: str, audit_path: Path, audit: Callable[..., None],
        error_writer: ErrorSnapshotWriter | None = None,
        error_validator: Callable[[Path, Path], Any] | None = None,
        compatibility_validator: Callable[..., Any] | None = None,
        manual_record: CodeSessionRecord | None = None,
    ) -> tuple[str, str, int]:
        feedback = ""
        diagnostic = ""
        for attempt in range(1, request.repair_policy.max_attempts + 1):
            await self._update_ephemeral_context(
                runtime,
                request=request,
                worktree=worktree,
                attempt=attempt,
                test_file=test_file,
                feedback=feedback,
            )
            prompt = (
                self._prompt(request, attempt, feedback, test_file)
                if getattr(runtime, "harness_dynamic_context", None) is not None
                else self._legacy_compatibility_prompt(request, attempt, feedback, test_file)
            )
            audit("coding_attempt", attempt=attempt, trigger=request.trigger)
            try:
                diagnostic = await self._invoke_runtime(runtime, prompt)
            except Exception as exc:
                feedback = f"Coding Runtime failed: {str(exc) or type(exc).__name__}"
                audit("coding_runtime_failed", attempt=attempt, message=feedback)
                if not request.repair_policy.allow_automatic_repair:
                    return diagnostic, feedback, attempt
                continue
            if compatibility_validator is not None and manual_record is not None:
                validation = await compatibility_validator(manual_record, test_file)
                valid, feedback = bool(validation["passed"]), str(validation["feedback"])
            else:
                valid, feedback = await self._validate_target(
                    worktree, request, audit_path, test_file,
                    error_writer=error_writer, error_validator=error_validator,
                )
            if valid:
                return diagnostic, "", attempt
            audit("validation_failed", attempt=attempt, feedback=feedback)
            if not request.repair_policy.allow_automatic_repair:
                return diagnostic, feedback, attempt
        return diagnostic, feedback or "validation did not pass", request.repair_policy.max_attempts

    async def _update_ephemeral_context(
        self,
        runtime: Any,
        *,
        request: HarnessEvolutionRequest,
        worktree: Path,
        attempt: int,
        test_file: str,
        feedback: str,
    ) -> None:
        controller = getattr(runtime, "harness_dynamic_context", None)
        if controller is None:
            return
        status = await self._command(
            worktree,
            ["git", "status", "--porcelain", "--untracked-files=all"],
            check=False,
        )
        head = await self._command(worktree, ["git", "rev-parse", "HEAD"], check=False)
        origin_refs: dict[str, Any] = {
            "task_hash": hashlib.sha256(request.task.encode("utf-8")).hexdigest(),
        }
        if request.origin is not None:
            origin_refs["origin"] = request.origin.model_dump(mode="json")
        if request.snapshot_path is not None:
            snapshot = request.snapshot_path.resolve()
            origin_refs["error_snapshot"] = {
                "path": str(snapshot),
                "content_hash": hashlib.sha256(snapshot.read_bytes()).hexdigest()
                if snapshot.is_file() else "",
            }
        if request.capability_gap is not None:
            origin_refs["capability_gap"] = request.capability_gap.model_dump(mode="json")
        if request.dream_context is not None:
            origin_refs["dream"] = request.dream_context.model_dump(mode="json")
        git_state = {
            "head": head.stdout.strip() if head.returncode == 0 else "",
            "status": status.stdout[-12000:] if status.returncode == 0 else "unavailable",
        }
        source_payload = json.dumps(
            {"origin_refs": origin_refs, "git_state": git_state, "attempt": attempt},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        controller.update(
            origin_refs=_sanitize(origin_refs),
            worktree_state={
                "isolated": True,
                "workspace_name": worktree.name,
            },
            git_state=_sanitize(git_state),
            current_attempt=attempt,
            assigned_validation={"test_file": test_file} if test_file else {},
            previous_validation_summary=str(_sanitize(feedback[-12000:])),
            recovery_constraints=(
                "Do not repeat an UNKNOWN external or Git side effect.",
                "Do not modify durable history to make recovery appear successful.",
            ),
            source_hash=hashlib.sha256(source_payload.encode("utf-8")).hexdigest(),
        )

    async def _invoke_runtime(self, runtime: Any, prompt: str) -> str:
        session_id = str(getattr(runtime, "coding_session_id", "")) or None
        run_task = getattr(runtime, "run_task", None)
        if callable(run_task):
            answers: list[str] = []
            async for event in run_task(prompt, session_id=session_id):
                if event.type is EventType.FINAL:
                    answers.append(str(event.payload.get("answer", "")))
            return answers[-1] if answers else ""
        result = await runtime.run(prompt, session_id=session_id) if session_id else await runtime.run(prompt)
        return str(result.answer)

    def _prompt(
        self, request: HarnessEvolutionRequest, attempt: int, feedback: str, test_file: str,
    ) -> str:
        del feedback, test_file
        # Trigger rules live in the byte-stable system prefix. Attempt, validation, Git,
        # origin, and recovery facts are injected ephemerally into this current query.
        if attempt == 1:
            return request.task.strip()
        return (
            f"Continue the same Harness task without starting over (repair attempt {attempt}):\n"
            f"{request.task.strip()}"
        )

    def _legacy_compatibility_prompt(
        self,
        request: HarnessEvolutionRequest,
        attempt: int,
        feedback: str,
        test_file: str,
    ) -> str:
        """Keep injected two-argument test runtimes working for one compatibility release."""
        if request.trigger == "manual":
            return CodeSessionController._turn_prompt(request.task, test_file, attempt, feedback)
        if request.trigger == "capability":
            return self._capability_prompt(request, attempt, feedback, test_file)
        prompt = request.task
        if test_file:
            prompt += f"\n\nCreate and run the assigned test `{test_file}`."
        if feedback:
            prompt += f"\n\nPrevious validation failed:\n{feedback}"
        return prompt

    async def _update_shared_memory(
        self, root: Path, task: str, commit: str, changed_files: list[str],
        audit: Callable[..., None],
    ) -> None:
        long_term = HarnessLongTermMemory(
            self.config.agent_root / ".yy" / "harness-evolution" / "memory" / "profile",
            agent_root=self.config.agent_root,
        )
        long_term.ensure_project_initialized(root)
        update = long_term.deterministic_update(
            root, task=task, commit_sha=commit, changed_files=changed_files,
        )
        try:
            await long_term.apply_update(update)
            audit("long_term_memory_updated", mode="deterministic", changed_files=changed_files)
        except Exception as exc:
            audit("long_term_memory_failed", message=str(exc) or type(exc).__name__)

    async def _validate_target(
        self, worktree: Path, request: HarnessEvolutionRequest, audit_path: Path,
        test_file: str, *, error_writer: ErrorSnapshotWriter | None,
        error_validator: Callable[[Path, Path], Any] | None,
    ) -> tuple[bool, str]:
        status = (await self._command(
            worktree, ["git", "status", "--porcelain", "--untracked-files=all"],
        )).stdout
        paths = _status_paths(status)
        if not paths:
            return False, "Coding Agent produced no source changes"
        forbidden = _forbidden_changed_paths(status)
        if forbidden:
            return False, f"Forbidden Agent Home, Git, credential, or local path changed: {forbidden[0]}"
        if request.target == "source_repair":
            validator = error_validator
            if validator is None:
                commands = [
                    ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q"],
                    ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
                    ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "Agent", "bootstrap", "context_process", "dream", "extension", "gateway", "memory", "prompt", "reference", "sandbox", "skill", "tool", "tools", "run_ui", "tests", "harness-evolution", "run.py"],
                    ["uv", "lock", "--check"],
                    ["git", "diff", "--check"],
                ]
                for command in commands:
                    result = await self._command(worktree, command, check=False, timeout=1200)
                    self.audit.append(
                        audit_path, "validation", command=command,
                        returncode=result.returncode, stdout=result.stdout[-65536:],
                        stderr=result.stderr[-65536:],
                    )
                    if error_writer is not None and request.snapshot_path is not None:
                        error_writer.append_event(
                            request.snapshot_path, "test", command=command,
                            returncode=result.returncode,
                            stdout=result.stdout[-65536:], stderr=result.stderr[-65536:],
                        )
                    if result.returncode:
                        return False, f"{' '.join(command)} failed: {(result.stderr or result.stdout)[-4000:]}"
                return True, ""
            passed = bool(await validator(worktree, request.snapshot_path))
            return (True, "") if passed else (False, "Full ERROR validation pipeline failed")
        if request.target == "dream_optimize":
            return await self._validate_dream(worktree, request, audit_path, test_file)
        return await self._validate_capability(
            worktree, request, request.resolved_invocation_id(), audit_path, test_file,
        )

    async def _commit_candidate(self, worktree: Path, message: str) -> None:
        await self._command(worktree, ["git", "add", "--all"])
        await self._command(worktree, [
            "git", "-c", "user.name=Yuan Ye Harness",
            "-c", "user.email=harness@local.invalid", "commit", "-m", message,
        ])

    async def _merge_verified(
        self, *, root: Path, base: str, verified: str, branch: str,
        audit: Callable[..., None], invocation_id: str, trigger: str,
        target_branch: str | None = None,
    ) -> str:
        clean = await self._command(
            root, ["git", "status", "--porcelain", "--untracked-files=all"], check=False,
        )
        current = (await self._command(root, ["git", "rev-parse", "HEAD"])).stdout.strip()
        selected_branch = target_branch or (
            await self._command(root, ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        ).stdout.strip()
        if clean.returncode or clean.stdout.strip() or current != base:
            audit(
                "merge_blocked", invocation_id=invocation_id, trigger=trigger,
                expected_head=base, current_head=current, target_branch=selected_branch,
            )
            return "blocked_main_changed"
        changed_files = await self._changed_files(root, base, verified)
        audit(
            "merge_intent", invocation_id=invocation_id, trigger=trigger,
            source_identity=hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:16],
            base_commit=base, verified_commit=verified, candidate_branch=branch,
            target_branch=selected_branch, expected_head=base, changed_files=changed_files,
        )
        merged = await self._command(root, ["git", "merge", "--ff-only", branch], check=False)
        if merged.returncode:
            audit("merge_unknown", invocation_id=invocation_id, error=merged.stderr[-4096:])
            return "unknown"
        actual = (await self._command(root, ["git", "rev-parse", "HEAD"])).stdout.strip()
        if actual != verified:
            audit("merge_unknown", invocation_id=invocation_id, expected=verified, actual=actual)
            return "unknown"
        audit(
            "merge_committed", invocation_id=invocation_id,
            trigger=trigger,
            source_identity=hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:16],
            base_commit=base, verified_commit=verified, merged_commit=actual,
            target_branch=selected_branch, changed_files=changed_files,
        )
        return "merged"

    async def _validate_dream(
        self, worktree: Path, request: HarnessEvolutionRequest,
        audit_path: Path, test_file: str,
    ) -> tuple[bool, str]:
        context = request.dream_context
        if context is None:
            return False, "DREAM changeset is missing"
        status = (
            await HarnessEvolutionRunner._command(
                worktree, ["git", "status", "--porcelain", "--untracked-files=all"],
            )
        ).stdout
        paths = _status_paths(status)
        protected_exact = {
            "pyproject.toml", "uv.lock", "tool/contracts.py", "tool/registry.py",
            "gateway/state_controller.py", "gateway/harness_evolution.py",
            "gateway/harness_dream.py", "tools/harness_dream.py",
            "tools/harness_capability.py", "tools/harness_error.py", "tools/harness_manual.py",
        }
        protected_prefixes = (
            ".git/", ".yy/", ".yy-backups/", "backup/", "gateway/recovery.py",
            "gateway/maintenance", "harness-evolution/",
        )
        allowed = set(context.changeset.changed_files)
        allowed_tests = {path for path in allowed if path.startswith("tests/")}
        allowed.add(test_file)
        if any(path in protected_exact or path.startswith(protected_prefixes) for path in paths):
            return False, "requires broader source change: DREAM protected path modified"
        if any(path not in allowed and path not in allowed_tests for path in paths):
            return False, "DREAM target changed a path outside its immutable changeset allowlist"
        source_candidates = {
            path for path in context.changeset.changed_files if not path.startswith("tests/")
        }
        if not any(path in source_candidates for path in paths):
            return False, "DREAM produced no source optimization"
        if test_file not in paths or not (worktree / test_file).is_file():
            return False, f"DREAM must create its assigned regression test: {test_file}"
        commands = [
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q", test_file],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "Agent", "dream", "gateway", "harness-evolution", "tool", "tools"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            ["uv", "lock", "--check"],
            ["git", "diff", "--check"],
        ]
        for command in commands:
            result = await HarnessEvolutionRunner._command(
                worktree, command, check=False, timeout=1200,
            )
            self.audit.append(
                audit_path, "validation", command=command, returncode=result.returncode,
                stdout=result.stdout[-16000:], stderr=result.stderr[-16000:],
            )
            if result.returncode:
                return False, f"{' '.join(command)} failed: {(result.stderr or result.stdout)[-4000:]}"
        return True, ""

    async def _changed_files(self, root: Path, base: str, verified: str) -> list[str]:
        result = await self._command(root, ["git", "diff", "--name-only", f"{base}..{verified}"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    async def _command(
        directory: Path, command: list[str], *, check: bool = True, timeout: float = 1200,
    ) -> "_CommandResult":
        return await HarnessEvolutionRunner._command(
            directory, command, check=check, timeout=timeout,
        )

    @staticmethod
    def _capability_prompt(request: HarnessEvolutionRequest, attempt: int, feedback: str, test_file: str) -> str:
        gap = request.capability_gap
        scope = (
            "tools/**/*.py, tests/tools/**, tool/defaults.py and tools/__init__.py only when registration/export is necessary"
        )
        prompt = (
            "You are the Yuan Ye Harness Coding Agent in an isolated Git worktree. "
            f"This is a Tool capability evolution. Only modify {scope}. "
            "Do not modify Harness controls, Tool registry/contracts, Gateway core, pyproject.toml, uv.lock, credentials, .git or .yy. "
            "Any new Tool must declare `delegatable = False` and `runtime_profiles = ('interactive',)`. "
            f"User task: {request.task}\nCapability gap: "
            f"{json.dumps(gap.model_dump(mode='json') if gap else {}, ensure_ascii=False)}\n"
            f"Implement the smallest safe change and tests. The only permitted dedicated test path is `{test_file}`. "
            "If this needs a dependency, credential plumbing, database migration, or core framework change, stop and report that broader source change is required."
        )
        if attempt > 1:
            prompt += f"\nValidation failed previously:\n{feedback}\nRepair the same candidate; do not start over."
        return prompt

    async def _validate_capability(self, worktree: Path, request: HarnessEvolutionRequest, invocation_id: str, audit_path: Path, test_file: str) -> tuple[bool, str]:
        status = (await HarnessEvolutionRunner._command(worktree, ["git", "status", "--porcelain", "--untracked-files=all"])).stdout
        paths = _status_paths(status)
        protected = {"tool/contracts.py", "tool/registry.py", "pyproject.toml", "uv.lock"}
        if any(path in protected or path.startswith(("harness-evolution/", "gateway/", ".yy/", ".git/")) for path in paths):
            return False, "requires broader source change: protected path modified"
        if request.target == "tool":
            if not any(path.startswith("tools/") and path.endswith(".py") for path in paths):
                return False, "TOOL target must modify a tools/*.py implementation"
            if test_file not in paths or not (worktree / test_file).is_file():
                return False, f"TOOL target must create its assigned test file: {test_file}"
            if any(not (
                path.startswith("tools/")
                or path in {"tool/defaults.py", "tools/__init__.py"}
                or re.fullmatch(r"tests/tools/test_[a-z0-9_]+_[0-9a-f]{8}\.py", path)
            ) for path in paths):
                return False, "TOOL target changed a path outside its allowlist"
        else:
            if any(not (path.startswith("extension/hook/") or path.startswith("tests/extensions/")) for path in paths):
                return False, "EXTENSION target changed a path outside its allowlist"
            if not any(path.startswith("extension/hook/") for path in paths):
                return False, "EXTENSION target must modify a hook implementation"
        commands: list[list[str]] = []
        if request.target == "tool":
            commands.extend([
                ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q", test_file],
                ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "tool", "tools"],
            ])
        commands.extend([
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            ["uv", "lock", "--check"],
            ["git", "diff", "--check"],
        ])
        for command in commands:
            result = await HarnessEvolutionRunner._command(worktree, command, check=False, timeout=1200)
            self.audit.append(audit_path, "validation", command=command, returncode=result.returncode, stdout=result.stdout[-16000:], stderr=result.stderr[-16000:])
            if result.returncode:
                return False, f"{' '.join(command)} failed: {(result.stderr or result.stdout)[-4000:]}"
        if request.target == "tool":
            candidate = await self._registry_snapshot(worktree)
            baseline = self._tool_registry_baselines.get(invocation_id, {})
            for name, contract in baseline.items():
                if candidate.get(name) != contract:
                    return False, f"Unrelated default Tool contract changed or disappeared: {name}"
            added = sorted(set(candidate).difference(baseline))
            if not added and not any(path.startswith("tools/") for path in paths):
                return False, "TOOL target neither registered nor modified a Tool implementation"
            for name in added:
                contract = candidate[name]
                if contract.get("delegatable", True):
                    return False, f"New capability Tool must declare delegatable=False: {name}"
                if contract.get("runtime_profiles") != ["interactive"]:
                    return False, f"New capability Tool must declare runtime_profiles=('interactive',): {name}"
        return True, ""

    async def _registry_snapshot(self, root: Path) -> dict[str, Any]:
        script = (
            "import json; from pathlib import Path; from tool.defaults import default_tools; "
            f"r=default_tools(Path({str(root)!r}), runtime_profile='interactive'); "
            "print(json.dumps(r.contract_snapshot(), ensure_ascii=False, sort_keys=True))"
        )
        result = await self._command(
            root,
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-c", script],
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Default Tool Registry cannot be built: {(result.stderr or result.stdout)[-4000:]}")
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError("Default Tool Registry did not produce a valid contract snapshot") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Default Tool Registry contract snapshot must be an object")
        return value


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
            or lowered == ".yy-backups"
            or lowered.startswith(".yy-backups/")
            or lowered.startswith("tests/error/")
            or name.startswith(".env")
            or name in {"settings.local.json", "config.ini", "credentials.json", "secrets.json"}
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
