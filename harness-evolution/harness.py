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

from Agent import AgentRuntime, ModelRetryPolicy, RuntimeConfig, RuntimeFailure
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from Agent.runtime.subagent import RuntimeSubagentRunner
from memory import HarnessLongTermMemory, HarnessMemoryUpdate, MemoryStore
from prompt import compose_harness_memory_messages
from sandbox import SandboxSessionProtocol
from skill import SkillService
from tools import AsyncToolRegistry, SkillReadTool, default_tools, register_subagent


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
    skills = SkillService(isolated.agent_root, isolated.workspace_root)
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
    tools = default_tools(isolated.workspace_root)
    tools.register(SkillReadTool(skills))
    register_subagent(tools, RuntimeSubagentRunner(isolated, tools))
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
        )
        result = await runtime.run("维护 Harness Coding Agent 长期记忆")
        if not result.completed:
            raise RuntimeError("长期记忆维护 Runtime 未返回完整结果")
        return _parse_harness_memory_update(result.answer)

    async def _run_tests(self, worktree: Path, snapshot_path: Path) -> bool:
        commands = [
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "pytest", "-q"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            ["uv", "run", "--frozen", "--extra", "dev", "python", "-m", "compileall", "-q", "Agent", "bootstrap", "context_process", "memory", "prompt", "sandbox", "skill", "tools", "run_ui", "tests", "harness-evolution", "run.py"],
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
