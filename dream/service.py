"""Dream 两阶段记忆巩固、事务写入和顺序回滚。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from Agent.config import RuntimeConfig
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from Agent.retry import ModelRetryPolicy
from prompt import compose_dream_consolidation_messages, compose_dream_extraction_messages
from tool import AsyncToolRegistry
from sandbox import WorkspaceLockManager
from memory.long_term import MemoryScope, MemoryWriteRequest
from memory.retrieval import project_identity
from memory.structured import MemoryProfileProjector, MemoryWriter, StructuredMemoryStore

from .archive import SessionArchiveReader, contains_secret
from .execution import DreamExecutionJournal
from .models import (
    DreamCandidate,
    DreamCandidateList,
    DreamMemoryEntry,
    DreamMemoryIndex,
    DreamRollbackResult,
    DreamRunResult,
    DreamState,
    DreamStatus,
    DreamTranscriptRecord,
)


_MANAGED_START = "<!-- dream:managed:start -->"
_MANAGED_END = "<!-- dream:managed:end -->"
_SESSION_PROFILE = re.compile(r"^[0-9a-f]{16}\.md$")
ModelRunner = Callable[[list[dict[str, str]]], Awaitable[str]]


class _NoMemory:
    """Dream 维护 Runtime 不创建任何 Session JSONL。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def has_session(self, session_id: str) -> bool:
        return True

    def session_created_at(self, session_id: str) -> str:
        del session_id
        return _now()

    def active_path(self, session_id: str) -> Path:
        return self.root / ".yy" / "dream" / f"ephemeral-{session_id}.jsonl"

    def prompt_context(self, session_id: str | None = None) -> str:
        del session_id
        return ""

    def latest_summary(self, session_id: str) -> str:
        del session_id
        return ""


class DreamService:
    """从原始 Session 构建可验证、可回滚的全局 Profile 投影。"""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        provider_factory: Callable[[], Any] | None = None,
        model_runner: ModelRunner | None = None,
        excluded_sessions: Callable[[], set[str]] | None = None,
    ) -> None:
        self.config = config
        self.root = config.agent_root / ".yy" / "dream"
        self.profile_root = config.memory_dir / "profile"
        self.state_path = self.root / "state.json"
        self.memories_path = self.root / "memories.json"
        self.runs_root = self.root / "runs"
        self.executions_root = self.root / "executions"
        self.backups_root = self.root / "backups"
        self.transactions_root = self.root / "transactions"
        self.archive = SessionArchiveReader(config.memory_dir / "session")
        self.provider_factory = provider_factory or self._provider
        self.model_runner = model_runner
        self.excluded_sessions = excluded_sessions or (lambda: set())
        self._lock = asyncio.Lock()
        self.file_locks = WorkspaceLockManager(config.agent_root, state_root=config.agent_root)
        self._running = False
        self._input_tokens = 0
        self._output_tokens = 0
        self.memory_store = StructuredMemoryStore(config.memory_dir)
        self.memory_writer = MemoryWriter(self.memory_store)
        self.memory_projector = MemoryProfileProjector(self.memory_store)
        self._ensure()

    async def process_day(self, selected_date: date, *, run_id: str | None = None) -> DreamRunResult:
        async with self._lock:
            async with self.file_locks.write(self.state_path):
                self._running = True
                try:
                    return await self._process_range(
                        selected_date, selected_date, run_id=run_id,
                    )
                finally:
                    self._running = False

    async def process_pending(
        self, cutoff_date: date, *, run_id: str | None = None,
    ) -> DreamRunResult:
        """Consume every unprocessed user Evidence through one frozen cutoff."""
        async with self._lock:
            async with self.file_locks.write(self.state_path):
                self._running = True
                try:
                    return await self._process_range(
                        date.min, cutoff_date, run_id=run_id,
                    )
                finally:
                    self._running = False

    async def advance_if_no_pending(self, cutoff_date: date) -> DreamRunResult | None:
        """Advance the scan cursor without creating a Dream Run when there is no work."""
        async with self._lock:
            async with self.file_locks.write(self.state_path):
                state = self._state()
                archive = self.archive.iter_range(
                    date.min,
                    cutoff_date,
                    self.config.dream_timezone,
                    excluded_session_ids=self.excluded_sessions(),
                )
                fresh_by_day = _fresh_evidence_by_day(
                    archive.evidence, state, self.config.dream_timezone,
                )
                if any(fresh_by_day.values()):
                    return None
                state.last_completed_date = _max_date(
                    state.last_completed_date, cutoff_date.isoformat(),
                )
                state.last_attempted_date = cutoff_date.isoformat()
                state.last_status, state.last_error = "noop", None
                self._write_state(state)
                return DreamRunResult(
                    run_id="scan_" + hashlib.sha256(
                        cutoff_date.isoformat().encode("utf-8"),
                    ).hexdigest()[:24],
                    date=cutoff_date.isoformat(),
                    range_start=cutoff_date.isoformat(),
                    range_end=cutoff_date.isoformat(),
                    status="noop",
                    message="No unprocessed user evidence; scan cursor advanced",
                    sessions_processed=archive.session_count,
                    source_files_processed=archive.source_file_count,
                    records_processed=len(archive.records),
                    model=self.config.dream_model or self.config.model,
                    created_at=_now(),
                )

    async def backfill(self, start: date, end: date) -> tuple[DreamRunResult, ...]:
        if end < start:
            raise ValueError("Dream backfill 结束日期不能早于开始日期")
        if (end - start).days >= 31:
            raise ValueError("Dream 单次 backfill 最多处理 31 天")
        results: list[DreamRunResult] = []
        current = start
        while current <= end:
            results.append(await self.process_day(current))
            current = date.fromordinal(current.toordinal() + 1)
        return tuple(results)

    async def rollback(self, run_id: str | None = None) -> DreamRollbackResult:
        async with self._lock:
            async with self.file_locks.write(self.state_path):
                state = self._state()
                if not state.successful_runs:
                    return DreamRollbackResult(run_id=run_id or "", restored=False, message="没有可回滚的 Dream 运行")
                latest = state.successful_runs[-1]
                if run_id is not None and run_id != latest:
                    raise ValueError("只能按时间逆序回滚最近一次成功 Dream")
                backup = self.backups_root / latest
                if not backup.is_dir():
                    raise FileNotFoundError(f"Dream 备份不存在：{backup}")
                self._restore_backup(backup)
                self.memory_store.compensate_source(f"dream:{latest}:")
                _write_json_atomic(self.runs_root / f"rollback_{uuid4().hex}.json", {
                    "type": "rollback", "run_id": latest, "timestamp": _now(),
                })
                return DreamRollbackResult(
                    run_id=latest,
                    restored=True,
                    message=f"已回滚 Dream：{latest}",
                )

    def status(self, *, next_run_at: str | None = None) -> DreamStatus:
        state = self._state()
        return DreamStatus(
            enabled=self.config.dream_enabled,
            running=self._running,
            schedule=self.config.dream_schedule,
            timezone=self.config.dream_timezone,
            initialized_at=state.initialized_at,
            last_completed_date=state.last_completed_date,
            last_attempted_date=state.last_attempted_date,
            last_run_id=state.last_run_id,
            last_status=state.last_status,
            last_error=state.last_error,
            next_run_at=next_run_at,
        )

    async def record_external_failure(
        self,
        selected_date: date,
        *,
        run_id: str,
        error: str,
    ) -> DreamRunResult:
        """Persist a failure raised around the normal Dream processing body.

        Whole-run timeouts are enforced by the Gateway because they cover
        archive reads, custom runners and projections. Source evidence remains
        unconsumed, allowing a later scheduled pass to retry it safely.
        """
        async with self._lock:
            async with self.file_locks.write(self.state_path):
                result = DreamRunResult(
                    run_id=run_id,
                    date=selected_date.isoformat(),
                    range_start=selected_date.isoformat(),
                    range_end=selected_date.isoformat(),
                    status="failed",
                    message=f"Dream failed: {error}",
                    model=self.config.dream_model or self.config.model,
                    created_at=_now(),
                )
                state = self._state()
                state.last_run_id = run_id
                state.last_attempted_date = selected_date.isoformat()
                state.last_status = "failed"
                state.last_error = error
                self._write_state(state)
                self._write_run(result, candidates=[], rejected=[])
                return result

    async def _process_range(
        self,
        start_date: date,
        selected_date: date,
        *,
        run_id: str | None = None,
    ) -> DreamRunResult:
        self._input_tokens = 0
        self._output_tokens = 0
        run_id = run_id or uuid4().hex
        created_at = _now()
        archive = self.archive.iter_range(
            start_date,
            selected_date,
            self.config.dream_timezone,
            excluded_session_ids=self.excluded_sessions(),
        )
        state = self._state()
        fresh_by_day = _fresh_evidence_by_day(
            archive.evidence, state, self.config.dream_timezone,
        )
        fresh = {item for values in fresh_by_day.values() for item in values}
        effective_start = min(fresh_by_day) if fresh_by_day else selected_date.isoformat()
        if not fresh:
            result = DreamRunResult(
                run_id=run_id, date=selected_date.isoformat(), status="noop",
                range_start=effective_start, range_end=selected_date.isoformat(),
                message="该日期没有未处理的用户证据", sessions_processed=archive.session_count,
                source_files_processed=archive.source_file_count,
                records_processed=len(archive.records), created_at=created_at,
                model=self.config.dream_model or self.config.model,
            )
            state.last_completed_date = _max_date(state.last_completed_date, selected_date.isoformat())
            state.last_attempted_date = selected_date.isoformat()
            state.last_run_id, state.last_status, state.last_error = run_id, "noop", None
            self._write_state(state)
            self._write_run(result, candidates=[], rejected=[])
            return result

        profiles = self._profiles()
        records = _records_with_fresh_evidence(archive.records, fresh)
        execution = DreamExecutionJournal(self.executions_root, run_id)
        execution.append_once("session", "execution_started", {
            "range_start": effective_start,
            "range_end": selected_date.isoformat(),
            "evidence": [_evidence_reference(item) for item in archive.evidence
                         if item.evidence_id in fresh],
            "model": self.config.dream_model or self.config.model,
            "memoryless": True,
        })
        attempts = 0
        extracted: list[DreamCandidate] = []
        rejected: list[dict[str, str]] = []
        try:
            for batch_number, batch in enumerate(
                _batch_records(records, self.config.dream_batch_tokens), 1,
            ):
                output, used = await self._validated_call(
                    lambda error: compose_dream_extraction_messages(
                        [item.model_dump(mode="json") for item in batch], profiles, error,
                    ),
                    phase=f"extraction:{batch_number}",
                    execution=execution,
                )
                attempts += used
                extracted.extend(output.candidates)
            extracted, invalid = _validate_candidates(
                extracted, set(profiles), fresh, self._memories(), phase="extraction",
            )
            rejected.extend(invalid)
            consolidated: list[DreamCandidate] = []
            if extracted:
                memories = self._memories()
                output, used = await self._validated_call(
                    lambda error: compose_dream_consolidation_messages(
                        [item.model_dump(mode="json") for item in extracted],
                        [item.model_dump(mode="json") for item in memories.memories.values()],
                        profiles,
                        error,
                    ),
                    phase="consolidation",
                    execution=execution,
                )
                attempts += used
                consolidated, invalid = _validate_candidates(
                    list(output.candidates), set(profiles), fresh, memories,
                    phase="consolidation",
                )
                rejected.extend(invalid)
            memories = self._memories()
            changed = _apply_candidates(memories, consolidated, selected_date, run_id)
            for day, evidence_ids in fresh_by_day.items():
                already = set(state.processed_evidence.get(day, []))
                state.processed_evidence[day] = sorted(already | evidence_ids)
            state.last_completed_date = _max_date(state.last_completed_date, selected_date.isoformat())
            state.last_attempted_date = selected_date.isoformat()
            state.last_run_id, state.last_status, state.last_error = run_id, "completed", None
            state.successful_runs.append(run_id)
            result = DreamRunResult(
                run_id=run_id, date=selected_date.isoformat(), status="completed",
                range_start=effective_start, range_end=selected_date.isoformat(),
                execution_session_id=execution.execution_session_id,
                message=f"Dream 完成：处理 {len(fresh)} 条用户证据，更新 {changed} 条长期记忆",
                sessions_processed=archive.session_count,
                source_files_processed=archive.source_file_count,
                records_processed=len(records), evidence_processed=len(fresh),
                memories_changed=changed, attempts=attempts,
                input_tokens=self._input_tokens, output_tokens=self._output_tokens,
                model=self.config.dream_model or self.config.model,
                created_at=created_at,
            )
            self._commit(run_id, state, memories, profiles, result, consolidated, rejected)
            execution.append_once("result", "execution_completed", {
                "status": result.status,
                "evidence_processed": result.evidence_processed,
                "memories_changed": result.memories_changed,
                "candidate_count": len(consolidated),
            })
            return result
        except asyncio.CancelledError:
            # Cancellation is a maintenance/recovery boundary, not a failed
            # Dream conclusion.  Leave the source Evidence unconsumed so the
            # next scheduled pass can safely process it again.
            execution.append_once("result", "execution_interrupted", {
                "reason": "cooperative_maintenance_or_shutdown",
                "attempts": attempts,
            })
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            failed = DreamRunResult(
                run_id=run_id, date=selected_date.isoformat(), status="failed",
                range_start=effective_start, range_end=selected_date.isoformat(),
                execution_session_id=execution.execution_session_id,
                message=f"Dream 失败：{error}", sessions_processed=archive.session_count,
                source_files_processed=archive.source_file_count,
                records_processed=len(records), evidence_processed=len(fresh),
                attempts=attempts, model=self.config.dream_model or self.config.model,
                input_tokens=self._input_tokens, output_tokens=self._output_tokens,
                created_at=created_at,
            )
            state.last_run_id, state.last_status, state.last_error = run_id, "failed", error
            state.last_attempted_date = selected_date.isoformat()
            self._write_state(state)
            self._write_run(failed, candidates=extracted, rejected=rejected)
            execution.append_once("result", "execution_failed", {
                "error_type": type(exc).__name__,
                "error": error[:1000],
                "attempts": attempts,
            })
            return failed

    async def _validated_call(
        self,
        messages_factory: Callable[[str], list[dict[str, str]]],
        *,
        phase: str,
        execution: DreamExecutionJournal,
    ) -> tuple[DreamCandidateList, int]:
        error = ""
        for attempt in range(1, 4):
            try:
                messages = messages_factory(error)
                input_hash = hashlib.sha256(json.dumps(
                    messages, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                execution.append_once(
                    f"{phase}:attempt:{attempt}:started", "model_attempt_started",
                    {"phase": phase, "attempt": attempt, "input_hash": input_hash,
                     "message_count": len(messages)},
                )
                raw = await asyncio.wait_for(
                    self._run_model(messages),
                    timeout=float(self.config.dream_model_timeout_seconds),
                )
                parsed = DreamCandidateList.model_validate_json(_json_text(raw))
                execution.append_once(
                    f"{phase}:attempt:{attempt}:completed", "model_attempt_completed",
                    {"phase": phase, "attempt": attempt,
                     "output_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                     "candidates": [item.model_dump(mode="json")
                                    for item in parsed.candidates]},
                )
                return parsed, attempt
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                execution.append_once(
                    f"{phase}:attempt:{attempt}:failed", "model_attempt_failed",
                    {"phase": phase, "attempt": attempt,
                     "error_type": type(exc).__name__, "error": error[:1000]},
                )
        raise RuntimeError(f"Dream 模型连续三次未返回合法结构：{error}")

    async def _run_model(self, messages: list[dict[str, str]]) -> str:
        if self.model_runner is not None:
            self._input_tokens += _estimate_tokens(json.dumps(messages, ensure_ascii=False))
            output = await self.model_runner(messages)
            self._output_tokens += _estimate_tokens(output)
            return output
        from Agent.runtime.engine import AgentRuntime

        hooks = HookRegistry()

        async def inject(event: HookEvent) -> None:
            event.data["messages"] = [dict(item) for item in messages]
            event.data["tools"] = []

        async def capture_usage(event: HookEvent) -> None:
            metric = event.data.get("model_call")
            if not isinstance(metric, dict):
                return
            input_tokens = metric.get("input_tokens")
            if isinstance(input_tokens, dict):
                value = input_tokens.get("context_total")
                if isinstance(value, int):
                    self._input_tokens += value
            value = metric.get("output_tokens")
            if isinstance(value, int):
                self._output_tokens += value

        hooks.register(HookPoint.MODEL_BEFORE, inject, priority=-100)
        hooks.register(HookPoint.MODEL_AFTER, capture_usage, priority=100)
        selected = self.config.model_copy(update={
            "model": self.config.dream_model or self.config.model,
            "stream": False,
            "compression_threshold_tokens": 0,
        })
        runtime = AgentRuntime(
            selected,
            provider=self.provider_factory(),
            tools=AsyncToolRegistry(),
            memory=_NoMemory(selected.agent_root),
            hooks=hooks,
            enable_context_processing=False,
            enable_skills=False,
            enable_subagent=False,
            enable_sandbox=False,
            enable_extensions=False,
            enable_cron=False,
            enable_references=False,
            enable_paper_library=False,
            retry_policy=ModelRetryPolicy(max_attempts=3, delay_seconds=2),
            raise_errors=True,
        )
        try:
            result = await runtime.run("执行每日 Dream 记忆维护")
        finally:
            # Each Dream model attempt owns a memoryless Runtime.  Always emit
            # TRACE_END and release provider/hook resources, including when a
            # maintenance pre-drain cancels the in-flight provider request.
            await runtime.close()
        if not result.completed:
            raise RuntimeError("Dream 维护 Runtime 未返回完整结果")
        return result.answer

    async def run_stateless_model(self, messages: list[dict[str, str]]) -> str:
        """供其他Dream阶段复用同一无工具、无Memory、无Sandbox的模型边界。"""
        return await self._run_model(messages)

    def _provider(self):
        return build_provider(
            self.config.provider,
            self.config.dream_model or self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            stream=False,
            use_system_proxy=self.config.use_system_proxy,
            proxy_url=self.config.proxy_url,
        )

    def _profiles(self) -> dict[str, str]:
        self.profile_root.mkdir(parents=True, exist_ok=True)
        for name in ("USER.md", "RESEARCH.md", "OTHERS.md"):
            (self.profile_root / name).touch(exist_ok=True)
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(self.profile_root.glob("*.md"))
            if not _SESSION_PROFILE.fullmatch(path.name) and not path.is_symlink()
        }

    def _state(self) -> DreamState:
        return DreamState.model_validate_json(self.state_path.read_text(encoding="utf-8"), strict=True)

    def _memories(self) -> DreamMemoryIndex:
        return DreamMemoryIndex.model_validate_json(
            self.memories_path.read_text(encoding="utf-8"), strict=True,
        )

    def _write_state(self, state: DreamState) -> None:
        _write_model_atomic(self.state_path, state)

    def _write_run(
        self,
        result: DreamRunResult,
        *,
        candidates: list[DreamCandidate],
        rejected: list[dict[str, str]],
    ) -> None:
        _write_json_atomic(self.runs_root / f"{result.date}_{result.run_id}.json", {
            **result.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "rejected": rejected,
        })

    def _commit(
        self,
        run_id: str,
        state: DreamState,
        memories: DreamMemoryIndex,
        profiles: dict[str, str],
        result: DreamRunResult,
        candidates: list[DreamCandidate],
        rejected: list[dict[str, str]],
    ) -> None:
        affected = [self.state_path, self.memories_path]
        rendered: dict[Path, str] = {}
        for name, original in profiles.items():
            active = [
                item for item in memories.memories.values()
                if item.status == "active" and item.target_file == name
            ]
            rendered[self.profile_root / name] = _render_profile(original, active)
            affected.append(self.profile_root / name)
        backup = self._create_backup(run_id, affected)
        transaction = self.transactions_root / f"{run_id}.json"
        _write_json_atomic(transaction, {"run_id": run_id, "backup": str(backup), "status": "prepared"})
        try:
            # Canonical memory is committed independently from legacy files.
            # The source-ref gives rollback a deterministic compensation key.
            for candidate in candidates:
                scope, scope_key = self._candidate_scope(candidate.target_file)
                for evidence_id in candidate.evidence_ids:
                    self.memory_writer.write(MemoryWriteRequest(
                        scope=scope,
                        scope_key=scope_key,
                        kind=self._candidate_kind(candidate.target_file),
                        content=candidate.statement,
                        source="dream_consolidated",
                        source_ref=f"dream:{run_id}:{evidence_id}",
                        confidence=candidate.confidence,
                        # Dream output has no structural SINGLE subject identity;
                        # therefore update/supersede never auto-replaces a fact.
                        replace_existing=False,
                        locator=f"candidate:{candidate.target_file}",
                    ))
            for path, content in rendered.items():
                _write_text_atomic(path, content)
            _write_model_atomic(self.memories_path, memories)
            _write_model_atomic(self.state_path, state)
            self._write_run(result, candidates=candidates, rejected=rejected)
            transaction.unlink(missing_ok=True)
        except Exception:
            self.memory_store.compensate_source(f"dream:{run_id}:")
            self._restore_backup(backup)
            transaction.unlink(missing_ok=True)
            raise

    def _candidate_scope(self, target_file: str) -> tuple[MemoryScope, str]:
        if target_file.upper().startswith("PROJECT"):
            return MemoryScope.PROJECT, project_identity(str(self.config.workspace_root))
        if _SESSION_PROFILE.fullmatch(target_file):
            return MemoryScope.SESSION, Path(target_file).stem
        return MemoryScope.USER, "local-user"

    @staticmethod
    def _candidate_kind(target_file: str) -> str:
        upper = target_file.upper()
        if upper == "RESEARCH.MD":
            return "research"
        if upper == "OTHERS.MD":
            return "other"
        return "profile"

    def _create_backup(self, run_id: str, paths: list[Path]) -> Path:
        backup = self.backups_root / run_id
        backup.mkdir(parents=True, exist_ok=False)
        manifest: list[dict[str, Any]] = []
        for number, path in enumerate(paths):
            relative = path.resolve().relative_to(self.config.agent_root.resolve())
            existed = path.exists()
            stored = f"{number:03d}.bin"
            if existed:
                (backup / stored).write_bytes(path.read_bytes())
            manifest.append({"path": str(relative), "existed": existed, "stored": stored})
        _write_json_atomic(backup / "manifest.json", {"run_id": run_id, "files": manifest})
        return backup

    def _restore_backup(self, backup: Path) -> None:
        value = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        for item in value["files"]:
            destination = (self.config.agent_root / item["path"]).resolve()
            destination.relative_to(self.config.agent_root.resolve())
            if item["existed"]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(destination.suffix + ".dream-restore.tmp")
                temporary.write_bytes((backup / item["stored"]).read_bytes())
                temporary.replace(destination)
            else:
                destination.unlink(missing_ok=True)

    def _ensure(self) -> None:
        for path in (
            self.root,
            self.runs_root,
            self.executions_root,
            self.backups_root,
            self.transactions_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            _write_model_atomic(self.state_path, DreamState(initialized_at=_now()))
        if not self.memories_path.exists():
            _write_model_atomic(self.memories_path, DreamMemoryIndex())
        for transaction in self.transactions_root.glob("*.json"):
            try:
                value = json.loads(transaction.read_text(encoding="utf-8"))
                self._restore_backup(Path(value["backup"]))
            finally:
                transaction.unlink(missing_ok=True)


def _records_with_fresh_evidence(
    records: tuple[DreamTranscriptRecord, ...],
    fresh: set[str],
) -> list[DreamTranscriptRecord]:
    blocks: list[list[DreamTranscriptRecord]] = []
    for record in records:
        if record.role == "user":
            blocks.append([record])
        elif blocks:
            blocks[-1].append(record)
    return [item for block in blocks if any(item.evidence_id in fresh for item in block) for item in block]


def _fresh_evidence_by_day(
    evidence: tuple[Any, ...],
    state: DreamState,
    timezone_name: str,
) -> dict[str, set[str]]:
    """Group only unconsumed Evidence by its already-normalized local day."""
    del timezone_name  # Archive timestamps are already normalized to the configured zone.
    grouped: dict[str, set[str]] = {}
    for item in evidence:
        day = datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")).date().isoformat()
        if item.evidence_id in set(state.processed_evidence.get(day, ())):
            continue
        grouped.setdefault(day, set()).add(item.evidence_id)
    return grouped


def _evidence_reference(item: Any) -> dict[str, Any]:
    """Return a replay locator without copying private transcript content."""
    return {
        "evidence_id": item.evidence_id,
        "workspace_key": item.workspace_key,
        "session_id": item.session_id,
        "source_file": item.source_file,
        "line_number": item.line_number,
        "timestamp": item.timestamp,
        "content_hash": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
    }


def _batch_records(records: list[DreamTranscriptRecord], max_tokens: int) -> list[list[DreamTranscriptRecord]]:
    blocks: list[list[DreamTranscriptRecord]] = []
    for record in records:
        if record.role == "user" or not blocks:
            blocks.append([record])
        else:
            blocks[-1].append(record)
    batches: list[list[DreamTranscriptRecord]] = []
    current: list[DreamTranscriptRecord] = []
    current_tokens = 0
    for block in blocks:
        size = max(1, sum(len(item.content) for item in block) // 4)
        if current and current_tokens + size > max_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.extend(block)
        current_tokens += size
    if current:
        batches.append(current)
    return batches


def _validate_candidates(
    candidates: list[DreamCandidate],
    profile_names: set[str],
    evidence_ids: set[str],
    memories: DreamMemoryIndex,
    *,
    phase: str,
) -> tuple[list[DreamCandidate], list[dict[str, str]]]:
    accepted: list[DreamCandidate] = []
    rejected: list[dict[str, str]] = []
    for item in candidates:
        reason = ""
        if item.target_file not in profile_names:
            reason = "目标 Profile 不存在"
        elif not set(item.evidence_ids).issubset(evidence_ids):
            reason = "包含非用户或未知证据"
        elif contains_secret(item.statement) or "[REDACTED]" in item.statement:
            reason = "候选包含凭据或脱敏占位"
        elif item.operation in {"update", "supersede"} and item.memory_id not in memories.memories:
            reason = "引用的旧记忆不存在"
        elif phase == "extraction" and item.operation != "insert":
            reason = "抽取阶段只能生成 insert 候选"
        if reason:
            rejected.append({"statement": item.statement, "reason": reason})
        else:
            accepted.append(item)
    return accepted, rejected


def _apply_candidates(
    index: DreamMemoryIndex,
    candidates: list[DreamCandidate],
    selected_date: date,
    run_id: str,
) -> int:
    changed = 0
    day = selected_date.isoformat()
    for candidate in candidates:
        normalized = " ".join(candidate.statement.split()).casefold()
        duplicate = next((
            item for item in index.memories.values()
            if item.status == "active" and item.target_file == candidate.target_file
            and " ".join(item.statement.split()).casefold() == normalized
        ), None)
        if candidate.operation == "insert" and duplicate is not None:
            evidence = tuple(sorted(set(duplicate.evidence_ids) | set(candidate.evidence_ids)))
            index.memories[duplicate.memory_id] = duplicate.model_copy(update={
                "evidence_ids": evidence,
                "confidence": max(duplicate.confidence, candidate.confidence),
                "last_seen_date": day,
                "run_id": run_id,
            })
            changed += 1
            continue
        if candidate.operation == "update" and candidate.memory_id:
            old = index.memories[candidate.memory_id]
            index.memories[candidate.memory_id] = old.model_copy(update={
                "statement": candidate.statement,
                "target_file": candidate.target_file,
                "evidence_ids": tuple(sorted(set(old.evidence_ids) | set(candidate.evidence_ids))),
                "confidence": max(old.confidence, candidate.confidence),
                "last_seen_date": day,
                "run_id": run_id,
            })
            changed += 1
            continue
        memory_id = _memory_id(candidate.target_file, candidate.statement, run_id)
        entry = DreamMemoryEntry(
            memory_id=memory_id,
            target_file=candidate.target_file,
            statement=candidate.statement.strip(),
            evidence_ids=tuple(sorted(set(candidate.evidence_ids))),
            confidence=candidate.confidence,
            first_seen_date=day,
            last_seen_date=day,
            run_id=run_id,
        )
        if candidate.operation == "supersede" and candidate.memory_id:
            old = index.memories[candidate.memory_id]
            index.memories[candidate.memory_id] = old.model_copy(update={
                "status": "superseded", "superseded_by": memory_id, "run_id": run_id,
            })
        index.memories[memory_id] = entry
        changed += 1
    return changed


def _render_profile(original: str, entries: list[DreamMemoryEntry]) -> str:
    start = original.find(_MANAGED_START)
    end = original.find(_MANAGED_END)
    if (start < 0) != (end < 0) or (start >= 0 and end < start):
        raise ValueError("Profile 的 Dream 管理区标记损坏")
    lines = [_MANAGED_START, "## Dream 长期记忆", ""]
    for entry in sorted(entries, key=lambda item: (-item.confidence, item.memory_id)):
        lines.append(f"- {entry.statement} <!-- dream:id={entry.memory_id} -->")
    if not entries:
        lines.append("（暂无 Dream 长期记忆）")
    lines.append(_MANAGED_END)
    managed = "\n".join(lines)
    if start >= 0:
        managed_end = end + len(_MANAGED_END)
        return original[:start] + managed + original[managed_end:]
    separator = "" if not original or original.endswith("\n\n") else "\n" if original.endswith("\n") else "\n\n"
    return original + separator + managed + "\n"


def _memory_id(target: str, statement: str, run_id: str) -> str:
    digest = hashlib.sha256(f"{target}\0{statement}\0{run_id}".encode("utf-8")).hexdigest()[:16]
    return f"mem_{digest}"


def _json_text(raw: str) -> str:
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
        if value.startswith("json"):
            value = value[4:].lstrip()
    return value


def _estimate_tokens(value: str) -> int:
    if not value:
        return 0
    cjk = sum(1 for char in value if "\u3400" <= char <= "\u9fff")
    return cjk + (len(value) - cjk + 3) // 4


def _max_date(current: str | None, candidate: str) -> str:
    return candidate if current is None or candidate > current else current


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_model_atomic(path: Path, model: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    temporary.replace(path)
