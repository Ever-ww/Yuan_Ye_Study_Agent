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

from .archive import SessionArchiveReader, contains_secret
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
        self._ensure()

    async def process_day(self, selected_date: date) -> DreamRunResult:
        async with self._lock:
            async with self.file_locks.write(self.state_path):
                self._running = True
                try:
                    return await self._process_day(selected_date)
                finally:
                    self._running = False

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
            last_run_id=state.last_run_id,
            last_status=state.last_status,
            last_error=state.last_error,
            next_run_at=next_run_at,
        )

    async def _process_day(self, selected_date: date) -> DreamRunResult:
        self._input_tokens = 0
        self._output_tokens = 0
        run_id = uuid4().hex
        created_at = _now()
        archive = self.archive.iter_day(
            selected_date,
            self.config.dream_timezone,
            excluded_session_ids=self.excluded_sessions(),
        )
        state = self._state()
        already = set(state.processed_evidence.get(selected_date.isoformat(), []))
        fresh = {item.evidence_id for item in archive.evidence if item.evidence_id not in already}
        if not fresh:
            result = DreamRunResult(
                run_id=run_id, date=selected_date.isoformat(), status="noop",
                message="该日期没有未处理的用户证据", sessions_processed=archive.session_count,
                source_files_processed=archive.source_file_count,
                records_processed=len(archive.records), created_at=created_at,
                model=self.config.dream_model or self.config.model,
            )
            state.last_completed_date = _max_date(state.last_completed_date, selected_date.isoformat())
            state.last_run_id, state.last_status, state.last_error = run_id, "noop", None
            self._write_state(state)
            self._write_run(result, candidates=[], rejected=[])
            return result

        profiles = self._profiles()
        records = _records_with_fresh_evidence(archive.records, fresh)
        attempts = 0
        extracted: list[DreamCandidate] = []
        rejected: list[dict[str, str]] = []
        try:
            for batch in _batch_records(records, self.config.dream_batch_tokens):
                output, used = await self._validated_call(
                    lambda error: compose_dream_extraction_messages(
                        [item.model_dump(mode="json") for item in batch], profiles, error,
                    ),
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
                )
                attempts += used
                consolidated, invalid = _validate_candidates(
                    list(output.candidates), set(profiles), fresh, memories,
                    phase="consolidation",
                )
                rejected.extend(invalid)
            memories = self._memories()
            changed = _apply_candidates(memories, consolidated, selected_date, run_id)
            state.processed_evidence[selected_date.isoformat()] = sorted(already | fresh)
            state.last_completed_date = _max_date(state.last_completed_date, selected_date.isoformat())
            state.last_run_id, state.last_status, state.last_error = run_id, "completed", None
            state.successful_runs.append(run_id)
            result = DreamRunResult(
                run_id=run_id, date=selected_date.isoformat(), status="completed",
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
            return result
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            failed = DreamRunResult(
                run_id=run_id, date=selected_date.isoformat(), status="failed",
                message=f"Dream 失败：{error}", sessions_processed=archive.session_count,
                source_files_processed=archive.source_file_count,
                records_processed=len(records), evidence_processed=len(fresh),
                attempts=attempts, model=self.config.dream_model or self.config.model,
                input_tokens=self._input_tokens, output_tokens=self._output_tokens,
                created_at=created_at,
            )
            state.last_run_id, state.last_status, state.last_error = run_id, "failed", error
            self._write_state(state)
            self._write_run(failed, candidates=extracted, rejected=rejected)
            return failed

    async def _validated_call(
        self,
        messages_factory: Callable[[str], list[dict[str, str]]],
    ) -> tuple[DreamCandidateList, int]:
        error = ""
        for attempt in range(1, 4):
            try:
                raw = await self._run_model(messages_factory(error))
                return DreamCandidateList.model_validate_json(_json_text(raw)), attempt
            except Exception as exc:
                error = str(exc) or type(exc).__name__
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
        result = await runtime.run("执行每日 Dream 记忆维护")
        if not result.completed:
            raise RuntimeError("Dream 维护 Runtime 未返回完整结果")
        return result.answer

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
            for path, content in rendered.items():
                _write_text_atomic(path, content)
            _write_model_atomic(self.memories_path, memories)
            _write_model_atomic(self.state_path, state)
            self._write_run(result, candidates=candidates, rejected=rejected)
            transaction.unlink(missing_ok=True)
        except Exception:
            self._restore_backup(backup)
            transaction.unlink(missing_ok=True)
            raise

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
        for path in (self.root, self.runs_root, self.backups_root, self.transactions_root):
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
