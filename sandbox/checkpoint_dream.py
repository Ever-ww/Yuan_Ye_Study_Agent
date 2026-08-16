"""每日Dream中的无记忆checkpoint分支价值判断、合并与延迟GC。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .checkpoint import CheckpointStore
from .locks import WorkspaceLockManager
from .models import CheckpointValueAssessment


StatelessModelRunner = Callable[[list[dict[str, str]]], Awaitable[str]]
CheckpointCandidateValidator = Callable[[CheckpointStore, str], None]


class CheckpointDreamSessionResult(BaseModel):
    """一个Session在一次Dream tick中的结构化结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    session_id: str
    merged_branches: tuple[str, ...] = ()
    skipped_branches: tuple[str, ...] = ()
    blocked_branches: tuple[str, ...] = ()
    deferred_branches: tuple[str, ...] = ()
    unknown_branches: tuple[str, ...] = ()
    garbage_collected_branches: tuple[str, ...] = ()


class CheckpointDreamResult(BaseModel):
    """Checkpoint Dream独立阶段的汇总，不混入Profile/Harness Dream状态。"""

    model_config = ConfigDict(frozen=True, strict=True)

    selected_date: date
    sessions: tuple[CheckpointDreamSessionResult, ...] = ()


class CheckpointDreamCoordinator:
    """只在workspace精确位于Session活动HEAD时自动合并归档分支。"""

    def __init__(
        self,
        project_root: Path,
        state_root: Path,
        *,
        checkpoint_limit: int,
        merged_ref_retention_days: int = 30,
        model_runner: StatelessModelRunner | None = None,
        file_locks: WorkspaceLockManager | None = None,
        validators: Iterable[CheckpointCandidateValidator] = (),
    ) -> None:
        self.project_root = project_root.resolve()
        self.state_root = state_root.resolve()
        self.checkpoint_limit = checkpoint_limit
        self.merged_ref_retention_days = merged_ref_retention_days
        self.model_runner = model_runner
        self.validators = tuple(validators)
        self.file_locks = file_locks or WorkspaceLockManager(
            self.project_root, state_root=self.state_root,
        )

    async def process_due(self, selected_date: date) -> CheckpointDreamResult:
        """按Session和fork顺序处理；没有checkpoint状态时不创建任何运行产物。"""
        results: list[CheckpointDreamSessionResult] = []
        for session_id in CheckpointStore.discover_session_ids(self.project_root, self.state_root):
            async with self.file_locks.workspace_exclusive():
                store = self._store(session_id)
                collected = store.collect_merged_branch_refs()
                merged: list[str] = []
                skipped: list[str] = []
                blocked: list[str] = []
                deferred: list[str] = []
                unknown: list[str] = []
                branches = sorted(
                    (
                        item for item in store.list_branches()
                        if item.status == "archived" and item.merge_eligible
                        and item.merge_state == "ready"
                    ),
                    key=lambda item: (item.archived_at or item.created_at, item.branch_id),
                )
                for branch in branches:
                    if not store.workspace_matches_active():
                        store.defer_merge(branch.branch_id, "workspace_not_at_session_active_head")
                        deferred.append(branch.branch_id)
                        continue
                    try:
                        assessment = await self._assess(store.branch_value_input(branch.branch_id))
                    except Exception as exc:
                        store.defer_merge(branch.branch_id, f"value_model_unavailable:{type(exc).__name__}:{exc}")
                        deferred.append(branch.branch_id)
                        continue
                    updated = store.apply_value_assessment(branch.branch_id, assessment)
                    if assessment.decision == "SKIP":
                        skipped.append(branch.branch_id)
                        continue
                    if assessment.decision == "NEEDS_REVIEW":
                        blocked.append(branch.branch_id)
                        break
                    try:
                        record = store.merge_archived_branch(
                            branch.branch_id,
                            assessment,
                            validator=(
                                lambda commit_sha: self._validate_candidate(store, commit_sha)
                                if self.validators else None
                            ),
                        )
                    except RuntimeError as exc:
                        # ref/workspace身份异常可能位于外部副作用窗口，绝不自动猜测重试。
                        store.mark_merge_unknown(branch.branch_id, f"merge_unknown:{type(exc).__name__}:{exc}")
                        unknown.append(branch.branch_id)
                        break
                    if record is None:
                        current = next(item for item in store.list_branches() if item.branch_id == updated.branch_id)
                        if current.merge_state == "blocked":
                            blocked.append(branch.branch_id)
                            break
                        deferred.append(branch.branch_id)
                        continue
                    merged.append(branch.branch_id)
                if any((merged, skipped, blocked, deferred, unknown, collected)):
                    results.append(CheckpointDreamSessionResult(
                        session_id=session_id,
                        merged_branches=tuple(merged),
                        skipped_branches=tuple(skipped),
                        blocked_branches=tuple(blocked),
                        deferred_branches=tuple(deferred),
                        unknown_branches=tuple(unknown),
                        garbage_collected_branches=collected,
                    ))
        return CheckpointDreamResult(selected_date=selected_date, sessions=tuple(results))

    def reconcile(self, attempt_id: str) -> dict[str, Any]:
        """定位既有Session并核验Git事实；不会再次调用模型或执行merge。"""
        for session_id in CheckpointStore.discover_session_ids(self.project_root, self.state_root):
            store = self._store(session_id)
            if any(item.attempt_id == attempt_id for item in store.list_merge_attempts()):
                return {"session_id": session_id, **store.reconcile_merge_attempt(attempt_id)}
        raise KeyError(f"Checkpoint Dream merge attempt不存在：{attempt_id}")

    async def _assess(self, value_input: dict[str, Any]) -> CheckpointValueAssessment:
        if self.model_runner is None:
            raise RuntimeError("Checkpoint Dream未配置无记忆模型")
        prompt = (
            "你是Yuan Ye的无记忆Checkpoint Dream评估器。判断用户回退后保留的旧分支增量"
            "是否仍值得合并到当前活动分支。错误尝试、明显回退目标或高风险不确定变化不要自动合并。"
            "只能返回JSON：{decision:'MERGE|SKIP|NEEDS_REVIEW',reason:string,"
            "valuable_changes:string[],risk_summary:string}。\n输入："
            + json.dumps(value_input, ensure_ascii=False, sort_keys=True)
        )
        raw = await self.model_runner([
            {"role": "system", "content": "仅做只读价值判断，不拥有工具、Memory或文件写权限。"},
            {"role": "user", "content": prompt},
        ])
        return CheckpointValueAssessment.model_validate_json(_json_text(raw))

    def _validate_candidate(self, store: CheckpointStore, commit_sha: str) -> None:
        """在ref CAS前运行全部已注册验证器；任一失败都会把该Attempt标记为BLOCKED。"""
        for validator in self.validators:
            validator(store, commit_sha)

    def _store(self, session_id: str) -> CheckpointStore:
        store = CheckpointStore(
            self.project_root,
            state_root=self.state_root,
            limit=self.checkpoint_limit,
            merged_ref_retention_days=self.merged_ref_retention_days,
        )
        store.open(session_id)
        return store


class CheckpointBranchGarbageCollector:
    """为管理命令提供独立的merged branch ref GC入口。"""

    def __init__(self, coordinator: CheckpointDreamCoordinator) -> None:
        self.coordinator = coordinator

    def collect_due(self, *, now: datetime | None = None) -> dict[str, tuple[str, ...]]:
        removed: dict[str, tuple[str, ...]] = {}
        for session_id in CheckpointStore.discover_session_ids(
            self.coordinator.project_root, self.coordinator.state_root,
        ):
            store = self.coordinator._store(session_id)
            selected = store.collect_merged_branch_refs(now=now)
            if selected:
                removed[session_id] = selected
        return removed


def _json_text(value: str) -> str:
    selected = value.strip()
    if selected.startswith("```"):
        selected = selected.split("\n", 1)[1] if "\n" in selected else selected[3:]
        if selected.rstrip().endswith("```"):
            selected = selected.rstrip()[:-3]
    start, end = selected.find("{"), selected.rfind("}")
    return selected[start:end + 1] if start >= 0 and end >= start else selected


__all__ = [
    "CheckpointBranchGarbageCollector", "CheckpointCandidateValidator",
    "CheckpointDreamCoordinator",
    "CheckpointDreamResult", "CheckpointDreamSessionResult",
]
