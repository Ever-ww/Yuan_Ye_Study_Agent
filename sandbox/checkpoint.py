"""使用独立裸 Git 仓库保存可分叉、可合并的本地 workspace checkpoint。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from backup.security import SensitiveEnvSanitizer

from .models import (
    CheckpointAuditEvent,
    CheckpointBranchRecord,
    CheckpointMergeAttempt,
    CheckpointPendingMutation,
    CheckpointRecord,
    CheckpointRestorePoint,
    CheckpointState,
    CheckpointValueAssessment,
    RollbackResult,
)


_EXCLUDES = (
    ".git/", ".yy/", ".env", ".env.*", ".venv/", ".agents/", ".codex/",
    "__pycache__/", "*.py[cod]",
)
_ZERO_SHA = "0" * 40
_MERGED_REF_RETENTION_DAYS = 30
CandidateValidator = Callable[[str], None]


class CheckpointStore:
    """管理单个 Session 的隔离 checkpoint 分支图。

    分支仅存在于独立裸仓库；项目自身的 ``.git``、HEAD、index 和 branch 永不修改。
    ``restore_points`` 是受数量限制的用户入口，branch ref 则独立保护仍待 Dream 合并的内容。
    """

    def __init__(
        self,
        project_root: Path,
        *,
        state_root: Path | None = None,
        limit: int = 17,
        merged_ref_retention_days: int = _MERGED_REF_RETENTION_DAYS,
    ) -> None:
        if limit < 1:
            raise ValueError("checkpoint 上限必须大于等于 1")
        if merged_ref_retention_days < 1:
            raise ValueError("merged branch ref 保留天数必须大于等于 1")
        self.project_root = project_root.resolve()
        self.state_root = (state_root or project_root).resolve()
        self.limit = limit
        self.merged_ref_retention_days = merged_ref_retention_days
        self._session_id: str | None = None
        self._directory: Path | None = None
        self._git_dir: Path | None = None
        self._index_path: Path | None = None
        self._state_path: Path | None = None
        self._state: CheckpointState | None = None
        self._lock = threading.RLock()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def directory(self) -> Path:
        if self._directory is None:
            raise RuntimeError("CheckpointStore 尚未打开 Session")
        return self._directory

    @classmethod
    def discover_session_ids(cls, project_root: Path, state_root: Path) -> tuple[str, ...]:
        """只发现当前 workspace 对应的 checkpoint Session。"""
        project = project_root.resolve()
        state = state_root.resolve()
        base = state / ".yy" / "sandbox" / "checkpoints"
        if state != project:
            base /= _workspace_key(project)
        if not base.is_dir():
            return ()
        return tuple(sorted(path.name for path in base.iterdir() if (path / "index.json").is_file()))

    def open(self, session_id: str) -> None:
        """打开Session，必要时无损迁移v1并恢复未完成的rollback/merge。"""
        safe_id = _safe_session_id(session_id)
        with self._lock:
            directory = self._checkpoint_base() / safe_id
            git_dir = directory / "repository.git"
            directory.mkdir(parents=True, exist_ok=True)
            if not git_dir.exists():
                self._run_plain(["git", "init", "--bare", str(git_dir)])
            self._directory = directory
            self._git_dir = git_dir
            self._index_path = directory / "workspace.index"
            self._state_path = directory / "index.json"
            self._session_id = safe_id
            self._configure_repository()
            if self._state_path.exists():
                raw_text = self._state_path.read_text(encoding="utf-8")
                raw = json.loads(raw_text)
                self._state = self._migrate_v1(raw) if raw.get("version", 1) == 1 else (
                    CheckpointState.model_validate_json(raw_text, strict=True)
                )
                if self._state.session_id != safe_id:
                    raise ValueError("checkpoint 索引的 Session ID 不匹配")
                if Path(self._state.workspace_root).resolve() != self.project_root:
                    raise ValueError("checkpoint 索引绑定到了不同 workspace")
                self._recover_pending_mutation()
                self._validate_refs()
                self._cleanup_evicted_restore_refs()
            else:
                now = _now()
                branch = CheckpointBranchRecord(
                    branch_id="branch-000001",
                    ref="refs/yy/branches/branch-000001",
                    status="active",
                    created_at=now,
                )
                self._state = CheckpointState(
                    session_id=safe_id,
                    workspace_root=str(self.project_root),
                    active_branch_id=branch.branch_id,
                    branches=[branch],
                )
                self._write_state()
            head = self.active_branch().head_commit_sha
            self._load_index(head) if head else self._git(["read-tree", "--empty"])

    def create(
        self,
        source: str,
        metadata: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> CheckpointRecord | None:
        """在活动分支创建父子提交；无变化时不制造空恢复点。"""
        with self._lock:
            state = self._require_state()
            if state.pending_mutation is not None:
                raise RuntimeError("checkpoint mutation 尚未恢复，禁止创建新 checkpoint")
            branch = self.active_branch()
            previous = self._record(branch.head_commit_sha) if branch.head_commit_sha else None
            self._git(["add", "-A", "--", "."])
            tree_sha = self._git(["write-tree"]).strip()
            if previous is not None and tree_sha == previous.tree_sha and not force:
                return None
            sequence = state.next_sequence
            created_at = _now()
            arguments = ["commit-tree", tree_sha]
            if previous is not None:
                arguments.extend(["-p", previous.commit_sha])
            commit_sha = self._git(
                arguments,
                input_text=f"yy checkpoint {sequence}: {source}\n",
                identity=True,
            ).strip()
            pending = CheckpointPendingMutation(
                mutation_id=uuid4().hex,
                kind="create",
                stage="intent",
                old_branch_id=branch.branch_id,
                old_head_sha=branch.head_commit_sha,
                target_commit_sha=commit_sha,
                target_tree_sha=tree_sha,
                checkpoint_sequence=sequence,
                checkpoint_source=source,
                checkpoint_metadata=dict(metadata or {}),
                started_at=created_at,
            )
            state.pending_mutation = pending
            self._write_state()
            return self._finish_create(pending, recovered=False)

    def rollback(
        self,
        steps: int | None = None,
        *,
        sequence: int | None = None,
        checkpoint_sha: str | None = None,
        merge_eligible: bool = True,
        archive_reason: str = "user_rollback",
    ) -> RollbackResult:
        """保留原未来分支，并从目标恢复点创建新的活动分支。"""
        selectors = [steps is not None, sequence is not None, checkpoint_sha is not None]
        if sum(selectors) != 1:
            raise ValueError("steps、sequence、checkpoint_sha 必须且只能提供一个")
        if steps is not None and steps < 1:
            raise ValueError("回溯步数必须大于等于 1")
        with self._lock:
            state = self._require_state()
            if state.pending_mutation is not None:
                raise RuntimeError("已有未完成 checkpoint mutation")
            old_branch = self.active_branch()
            if old_branch.head_commit_sha is None:
                raise RuntimeError("当前活动分支尚无 checkpoint")
            target = self._rollback_target(steps, sequence, checkpoint_sha)
            if target.commit_sha == old_branch.head_commit_sha:
                raise ValueError("目标已经是当前活动 checkpoint")
            preserved = self._future_records(old_branch.head_commit_sha, target.commit_sha)
            now = _now()
            branch_id = f"branch-{state.next_branch_sequence:06d}"
            new_branch = CheckpointBranchRecord(
                branch_id=branch_id,
                ref=f"refs/yy/branches/{branch_id}",
                status="active",
                fork_checkpoint_sha=target.commit_sha,
                head_commit_sha=target.commit_sha,
                created_at=now,
            )
            pending = CheckpointPendingMutation(
                mutation_id=uuid4().hex,
                kind="rollback",
                stage="intent",
                old_branch_id=old_branch.branch_id,
                old_head_sha=old_branch.head_commit_sha,
                target_commit_sha=target.commit_sha,
                target_tree_sha=target.tree_sha,
                new_branch_id=branch_id,
                archived_branch_id=old_branch.branch_id,
                started_at=now,
            )
            state.pending_mutation = pending
            self._write_state()
            self._update_ref(new_branch.ref, target.commit_sha, None)
            archived = old_branch.model_copy(update={
                "status": "archived",
                "merge_eligible": merge_eligible,
                "merge_state": "ready" if merge_eligible else None,
                "archive_reason": archive_reason,
                "merge_eligibility_reason": "rollback_default" if merge_eligible else archive_reason,
                "fork_checkpoint_sha": target.commit_sha,
                "archived_at": now,
            })
            state.branches[:] = [
                archived if item.branch_id == old_branch.branch_id else item
                for item in state.branches
            ] + [new_branch]
            state.active_branch_id = branch_id
            state.next_branch_sequence += 1
            state.pending_mutation = pending.model_copy(update={"stage": "state_switched"})
            self._write_state()
            self._restore(target.commit_sha)
            self._verify_workspace_tree(target.tree_sha)
            state.pending_mutation = None
            state.events.extend([
                CheckpointAuditEvent(
                    action="rollback", timestamp=now, checkpoint_sha=target.commit_sha,
                    details={"from_branch": old_branch.branch_id, "to_branch": branch_id,
                             "preserved": [item.commit_sha for item in preserved]},
                ),
                CheckpointAuditEvent(
                    action="branch_forked", timestamp=now, checkpoint_sha=target.commit_sha,
                    details={"branch_id": branch_id, "archive_reason": archive_reason,
                             "merge_eligible": merge_eligible},
                ),
            ])
            self._write_state()
            return RollbackResult(
                restored=target,
                archived_branch=archived,
                new_active_branch=new_branch,
                preserved_future=tuple(preserved),
            )

    def restore_current(self) -> CheckpointRecord:
        with self._lock:
            branch = self.active_branch()
            if branch.head_commit_sha is None:
                raise RuntimeError("当前 Session 尚无可恢复 checkpoint")
            target = self._record(branch.head_commit_sha)
            self._restore(target.commit_sha)
            self._verify_workspace_tree(target.tree_sha)
            state = self._require_state()
            state.events.append(CheckpointAuditEvent(
                action="restored", timestamp=_now(), checkpoint_sha=target.commit_sha,
                details={"reason": "operation_failed", "branch_id": branch.branch_id},
            ))
            self._write_state()
            return target

    def list(self) -> tuple[CheckpointRecord, ...]:
        return self.list_checkpoints()

    def list_checkpoints(self, *, active_only: bool = False) -> tuple[CheckpointRecord, ...]:
        with self._lock:
            state = self._require_state()
            visible = {item.sequence for item in state.restore_points}
            selected = [item for item in state.commit_records if item.sequence in visible]
            if active_only:
                lineage = {item.commit_sha for item in self._active_lineage()}
                selected = [item for item in selected if item.commit_sha in lineage]
            return tuple(sorted(selected, key=lambda item: item.sequence))

    def list_branches(self) -> tuple[CheckpointBranchRecord, ...]:
        with self._lock:
            return tuple(self._require_state().branches)

    def list_merge_attempts(self) -> tuple[CheckpointMergeAttempt, ...]:
        with self._lock:
            return tuple(self._require_state().merge_attempts)

    def reconcile_merge_attempt(self, attempt_id: str) -> dict[str, Any]:
        """只读取既有attempt、branch ref和Git ancestry，不重新执行Dream。"""
        with self._lock:
            attempt = next(
                (item for item in self._require_state().merge_attempts if item.attempt_id == attempt_id),
                None,
            )
            if attempt is None:
                raise KeyError(f"merge attempt不存在：{attempt_id}")
            branch = self._branch(attempt.branch_id)
            active_head = self.active_branch().head_commit_sha
            if attempt.outcome == "merged" and attempt.candidate_commit and active_head:
                related = self._git_result([
                    "merge-base", "--is-ancestor", attempt.candidate_commit, active_head,
                ]).returncode == 0
                if branch.status == "merged" and related:
                    return {"status": "COMPLETED", "attempt_id": attempt_id,
                            "candidate_commit": attempt.candidate_commit}
                return {"status": "UNKNOWN", "attempt_id": attempt_id,
                        "reason": "merge evidence与活动历史不一致"}
            if attempt.outcome == "skipped":
                return {"status": "NOT_APPLIED", "attempt_id": attempt_id,
                        "reason": attempt.reason}
            if attempt.outcome in {"blocked", "deferred"}:
                return {"status": "FAILED", "attempt_id": attempt_id,
                        "reason": attempt.reason, "retryable": attempt.outcome == "deferred"}
            return {"status": "UNKNOWN", "attempt_id": attempt_id, "reason": attempt.reason}

    def active_branch(self) -> CheckpointBranchRecord:
        state = self._require_state()
        return next(item for item in state.branches if item.branch_id == state.active_branch_id)

    def workspace_matches_active(self) -> bool:
        with self._lock:
            branch = self.active_branch()
            if branch.head_commit_sha is None:
                return False
            return self._workspace_tree() == self._record(branch.head_commit_sha).tree_sha

    def set_merge_eligibility(self, branch_id: str, eligible: bool, reason: str) -> CheckpointBranchRecord:
        """显式改变归档分支是否允许进入Dream，且保留完整审计。"""
        if not reason.strip():
            raise ValueError("改变 merge eligibility 必须提供原因")
        with self._lock:
            state = self._require_state()
            branch = self._branch(branch_id)
            if branch.status != "archived":
                raise ValueError("只有 ARCHIVED 分支可以改变 merge eligibility")
            updated = branch.model_copy(update={
                "merge_eligible": eligible,
                "merge_state": "ready" if eligible else None,
                "merge_eligibility_reason": reason,
            })
            self._replace_branch(updated)
            state.events.append(CheckpointAuditEvent(
                action="merge_assessed", timestamp=_now(),
                checkpoint_sha=branch.head_commit_sha or branch.fork_checkpoint_sha or _ZERO_SHA,
                details={"branch_id": branch_id, "eligible": eligible, "reason": reason,
                         "actor": "explicit"},
            ))
            self._write_state()
            return updated

    def branch_value_input(self, branch_id: str) -> dict[str, Any]:
        """只暴露限长、脱敏后的差异摘要，不把凭据或完整文件送入Dream。"""
        with self._lock:
            branch = self._branch(branch_id)
            active = self.active_branch()
            if not branch.fork_checkpoint_sha or not branch.head_commit_sha or not active.head_commit_sha:
                raise ValueError("归档分支缺少可验证的fork/head")
            names = self._git([
                "diff", "--name-status", branch.fork_checkpoint_sha, branch.head_commit_sha,
            ])
            stat = self._git([
                "diff", "--stat", branch.fork_checkpoint_sha, branch.head_commit_sha,
            ])
            return {
                "session_id": self._session_id,
                "branch_id": branch.branch_id,
                "archive_reason": branch.archive_reason,
                "merge_eligibility_reason": branch.merge_eligibility_reason,
                "fork_commit": branch.fork_checkpoint_sha,
                "archived_head": branch.head_commit_sha,
                "active_head": active.head_commit_sha,
                "changed_paths": _redact_text(names)[:12000],
                "diff_stat": _redact_text(stat)[:4000],
            }

    def apply_value_assessment(
        self,
        branch_id: str,
        assessment: CheckpointValueAssessment,
    ) -> CheckpointBranchRecord:
        with self._lock:
            state = self._require_state()
            branch = self._branch(branch_id)
            if branch.status != "archived" or not branch.merge_eligible:
                raise ValueError("分支当前不允许Dream评估")
            if assessment.decision == "SKIP":
                updated = branch.model_copy(update={
                    "merge_eligible": False, "merge_state": None,
                    "merge_eligibility_reason": f"dream_skipped:{assessment.reason}",
                })
                self._append_merge_attempt(branch, "skipped", assessment.reason, assessment)
            elif assessment.decision == "NEEDS_REVIEW":
                updated = branch.model_copy(update={"merge_state": "blocked"})
                self._append_merge_attempt(branch, "blocked", assessment.reason, assessment)
            else:
                updated = branch.model_copy(update={"merge_state": "ready"})
            self._replace_branch(updated)
            state.events.append(CheckpointAuditEvent(
                action="merge_assessed", timestamp=_now(),
                checkpoint_sha=branch.head_commit_sha or branch.fork_checkpoint_sha or _ZERO_SHA,
                details={"branch_id": branch_id, **assessment.model_dump(mode="json")},
            ))
            self._write_state()
            return updated

    def defer_merge(self, branch_id: str, reason: str) -> None:
        with self._lock:
            branch = self._branch(branch_id)
            if branch.status != "archived" or not branch.merge_eligible:
                return
            self._replace_branch(branch.model_copy(update={"merge_state": "deferred"}))
            self._append_merge_attempt(branch, "deferred", reason, None)
            self._write_state()

    def mark_merge_unknown(self, branch_id: str, reason: str) -> None:
        """记录无法从现有Git事实确定结果的merge attempt。"""
        with self._lock:
            branch = self._branch(branch_id)
            if branch.status != "archived" or not branch.merge_eligible:
                return
            self._replace_branch(branch.model_copy(update={"merge_state": "unknown"}))
            self._append_merge_attempt(branch, "unknown", reason, None)
            self._write_state()

    def merge_archived_branch(
        self,
        branch_id: str,
        assessment: CheckpointValueAssessment,
        *,
        validator: CandidateValidator | None = None,
    ) -> CheckpointRecord | None:
        """对已通过价值判断的归档分支执行显式base三方合并。"""
        with self._lock:
            state = self._require_state()
            if state.pending_mutation is not None:
                raise RuntimeError("已有未完成 checkpoint mutation")
            branch = self._branch(branch_id)
            active = self.active_branch()
            if branch.status != "archived" or not branch.merge_eligible or branch.merge_state != "ready":
                raise ValueError("分支不处于READY合并状态")
            if not branch.fork_checkpoint_sha or not branch.head_commit_sha or not active.head_commit_sha:
                raise ValueError("分支缺少fork/head")
            if not self.workspace_matches_active():
                self.defer_merge(branch_id, "workspace_dirty_or_not_at_active_head")
                return None
            attempt_id = uuid4().hex
            candidate_tree, conflicts = self._merge_tree(
                branch.fork_checkpoint_sha, active.head_commit_sha, branch.head_commit_sha,
            )
            if conflicts:
                updated = branch.model_copy(update={"merge_state": "blocked"})
                self._replace_branch(updated)
                self._append_merge_attempt(
                    branch, "blocked", f"merge_conflict:{','.join(conflicts[:20])}", assessment,
                    attempt_id=attempt_id,
                )
                state.events.append(CheckpointAuditEvent(
                    action="merge_blocked", timestamp=_now(), checkpoint_sha=branch.head_commit_sha,
                    details={"branch_id": branch_id, "conflicts": conflicts[:20]},
                ))
                self._write_state()
                return None
            candidate_commit = self._git(
                ["commit-tree", candidate_tree, "-p", active.head_commit_sha,
                 "-p", branch.head_commit_sha],
                input_text=f"yy checkpoint dream merge {branch_id}\n",
                identity=True,
            ).strip()
            if validator is not None:
                try:
                    validator(candidate_commit)
                except Exception as exc:
                    self._replace_branch(branch.model_copy(update={"merge_state": "blocked"}))
                    self._append_merge_attempt(
                        branch, "blocked", f"validation_failed:{exc}", assessment,
                        candidate_commit=candidate_commit, attempt_id=attempt_id,
                    )
                    self._write_state()
                    return None
            sequence = state.next_sequence
            now = _now()
            pending = CheckpointPendingMutation(
                mutation_id=uuid4().hex,
                kind="merge",
                stage="intent",
                old_branch_id=active.branch_id,
                old_head_sha=active.head_commit_sha,
                target_commit_sha=candidate_commit,
                target_tree_sha=candidate_tree,
                archived_branch_id=branch_id,
                merge_attempt_id=attempt_id,
                merge_checkpoint_sequence=sequence,
                value_assessment=assessment.model_dump(mode="json"),
                started_at=now,
            )
            state.pending_mutation = pending
            self._write_state()
            checkpoint_ref = _checkpoint_ref(sequence)
            self._update_ref(checkpoint_ref, candidate_commit, None)
            self._update_ref(active.ref, candidate_commit, active.head_commit_sha)
            record = CheckpointRecord(
                sequence=sequence,
                commit_sha=candidate_commit,
                tree_sha=candidate_tree,
                ref=checkpoint_ref,
                source="checkpoint_dream_merge",
                created_at=now,
                changes=tuple(self._changes(active.head_commit_sha, candidate_commit)),
                metadata={"archived_branch_id": branch_id, "merge_attempt_id": attempt_id},
                branch_id=active.branch_id,
                parent_checkpoint_sha=active.head_commit_sha,
                merge_parent_sha=branch.head_commit_sha,
            )
            state.commit_records.append(record)
            state.restore_points.append(_restore_point(record))
            state.next_sequence += 1
            self._replace_branch(active.model_copy(update={"head_commit_sha": candidate_commit}))
            state.pending_mutation = pending.model_copy(update={"stage": "state_switched"})
            self._write_state()
            self._restore(candidate_commit)
            self._verify_workspace_tree(candidate_tree)
            merged_at = _now()
            self._replace_branch(branch.model_copy(update={
                "status": "merged", "merge_state": None, "merged_at": merged_at,
                "gc_after": merged_at + timedelta(days=self.merged_ref_retention_days),
            }))
            self._append_merge_attempt(
                branch, "merged", "validated_clean_merge", assessment,
                candidate_commit=candidate_commit, attempt_id=attempt_id,
                expected_active_head=active.head_commit_sha,
            )
            state.pending_mutation = None
            state.events.append(CheckpointAuditEvent(
                action="merge_committed", timestamp=merged_at, checkpoint_sha=candidate_commit,
                details={"branch_id": branch_id, "attempt_id": attempt_id,
                         "active_branch_id": active.branch_id},
            ))
            self._trim_restore_points()
            self._write_state()
            return record

    def collect_merged_branch_refs(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """30天后仅回收已经稳定合并且没有恢复引用的branch ref。"""
        with self._lock:
            state = self._require_state()
            if state.pending_mutation is not None:
                return ()
            selected_now = now or _now()
            active_head = self.active_branch().head_commit_sha
            if active_head is None:
                return ()
            removed: list[str] = []
            for branch in tuple(state.branches):
                if (
                    branch.status != "merged" or branch.ref_deleted_at is not None
                    or branch.gc_after is None or branch.gc_after > selected_now
                    or branch.head_commit_sha is None
                ):
                    continue
                ancestor = self._git_result([
                    "merge-base", "--is-ancestor", branch.head_commit_sha, active_head,
                ])
                if ancestor.returncode != 0:
                    continue
                self._delete_ref(branch.ref, missing_ok=True)
                updated = branch.model_copy(update={"ref_deleted_at": selected_now})
                self._replace_branch(updated)
                state.events.append(CheckpointAuditEvent(
                    action="branch_ref_gc", timestamp=selected_now,
                    checkpoint_sha=branch.head_commit_sha,
                    details={"branch_id": branch.branch_id, "retention_days": self.merged_ref_retention_days},
                ))
                removed.append(branch.branch_id)
            if removed:
                self._write_state()
                self._prune()
            return tuple(removed)

    # ---- 内部图、事务与Git辅助 ----

    def _rollback_target(
        self,
        steps: int | None,
        sequence: int | None,
        checkpoint_sha: str | None,
    ) -> CheckpointRecord:
        visible = self.list_checkpoints()
        if steps is not None:
            lineage = self._active_lineage()
            if steps >= len(lineage):
                raise ValueError(f"最多只能回溯 {max(len(lineage) - 1, 0)} 步")
            return lineage[steps]
        if sequence is not None:
            found = next((item for item in visible if item.sequence == sequence), None)
        else:
            found = next((item for item in visible if item.commit_sha == checkpoint_sha), None)
        if found is None:
            raise ValueError("指定checkpoint不存在或已超过可见恢复点保留上限")
        return found

    def _active_lineage(self) -> list[CheckpointRecord]:
        head = self.active_branch().head_commit_sha
        lineage: list[CheckpointRecord] = []
        seen: set[str] = set()
        while head:
            if head in seen:
                raise ValueError("checkpoint逻辑父链形成循环")
            seen.add(head)
            record = self._record(head)
            lineage.append(record)
            head = record.parent_checkpoint_sha
        return lineage

    def _future_records(self, head_sha: str, target_sha: str) -> list[CheckpointRecord]:
        selected: list[CheckpointRecord] = []
        current: str | None = head_sha
        while current and current != target_sha:
            record = self._record(current)
            selected.append(record)
            current = record.parent_checkpoint_sha
        if current != target_sha:
            # 精确跨分支fork时，原活动分支整体都是被保留的未来。
            return list(reversed(self._active_lineage()))
        return list(reversed(selected))

    def _recover_pending_mutation(self) -> None:
        pending = self._require_state().pending_mutation
        if pending is None:
            return
        if pending.kind == "create":
            self._finish_create(pending, recovered=True)
        elif pending.kind == "rollback":
            self._recover_rollback(pending)
        else:
            self._recover_merge(pending)

    def _finish_create(
        self,
        pending: CheckpointPendingMutation,
        *,
        recovered: bool,
    ) -> CheckpointRecord:
        """提交或恢复普通Checkpoint；先证明workspace仍是intent捕获的tree。"""
        state = self._require_state()
        sequence = pending.checkpoint_sequence
        source = pending.checkpoint_source
        if sequence is None or source is None:
            raise RuntimeError("create intent缺少checkpoint身份")
        if self._workspace_tree() != pending.target_tree_sha:
            raise RuntimeError(
                "checkpoint create恢复时workspace已出现外部变化；保留intent且拒绝覆盖",
            )
        branch = self._branch(pending.old_branch_id)
        checkpoint_ref = _checkpoint_ref(sequence)
        checkpoint_actual = self._resolve_ref(checkpoint_ref)
        if checkpoint_actual is None:
            self._update_ref(checkpoint_ref, pending.target_commit_sha, None)
        elif checkpoint_actual != pending.target_commit_sha:
            raise RuntimeError("checkpoint ref与create intent冲突")
        branch_actual = self._resolve_ref(branch.ref)
        if branch_actual == pending.old_head_sha:
            self._update_ref(branch.ref, pending.target_commit_sha, pending.old_head_sha)
        elif branch_actual != pending.target_commit_sha:
            raise RuntimeError("branch ref与create intent冲突")
        record = next(
            (item for item in state.commit_records if item.commit_sha == pending.target_commit_sha),
            None,
        )
        if record is None:
            record = CheckpointRecord(
                sequence=sequence,
                commit_sha=pending.target_commit_sha,
                tree_sha=pending.target_tree_sha,
                ref=checkpoint_ref,
                source=source,
                created_at=pending.started_at,
                changes=tuple(self._changes(pending.old_head_sha, pending.target_commit_sha)),
                metadata=pending.checkpoint_metadata,
                branch_id=branch.branch_id,
                parent_checkpoint_sha=pending.old_head_sha,
            )
            state.commit_records.append(record)
            state.restore_points.append(_restore_point(record))
            state.events.append(CheckpointAuditEvent(
                action="created", timestamp=pending.started_at,
                checkpoint_sha=pending.target_commit_sha,
                details={"source": source, "branch_id": branch.branch_id,
                         "changes": len(record.changes)},
            ))
        self._replace_branch(branch.model_copy(update={
            "head_commit_sha": pending.target_commit_sha,
        }))
        state.next_sequence = max(state.next_sequence, sequence + 1)
        state.pending_mutation = None
        if recovered:
            state.events.append(CheckpointAuditEvent(
                action="mutation_recovered", timestamp=_now(),
                checkpoint_sha=pending.target_commit_sha,
                details={"mutation_id": pending.mutation_id, "kind": pending.kind},
            ))
        self._trim_restore_points()
        self._write_state()
        return record

    def _recover_rollback(self, pending: CheckpointPendingMutation) -> None:
        state = self._require_state()
        if pending.old_head_sha is None:
            raise RuntimeError("rollback intent缺少旧活动HEAD")
        if not pending.new_branch_id:
            raise RuntimeError("rollback intent缺少new_branch_id")
        ref = f"refs/yy/branches/{pending.new_branch_id}"
        actual_ref = self._resolve_ref(ref)
        if actual_ref is None:
            self._update_ref(ref, pending.target_commit_sha, None)
        elif actual_ref != pending.target_commit_sha:
            raise RuntimeError("rollback branch ref与durable intent冲突")
        if state.active_branch_id != pending.new_branch_id:
            old = self._branch(pending.old_branch_id)
            now = pending.started_at
            archived = old.model_copy(update={
                "status": "archived", "merge_eligible": True, "merge_state": "ready",
                "archive_reason": "recovered_rollback", "archived_at": now,
                "merge_eligibility_reason": "rollback_recovered_default",
                "fork_checkpoint_sha": pending.target_commit_sha,
            })
            new_branch = CheckpointBranchRecord(
                branch_id=pending.new_branch_id, ref=ref, status="active",
                fork_checkpoint_sha=pending.target_commit_sha,
                head_commit_sha=pending.target_commit_sha, created_at=now,
            )
            self._replace_branch(archived)
            if not any(item.branch_id == new_branch.branch_id for item in state.branches):
                state.branches.append(new_branch)
            state.active_branch_id = new_branch.branch_id
            state.next_branch_sequence = max(state.next_branch_sequence, _branch_number(new_branch.branch_id) + 1)
            state.pending_mutation = pending.model_copy(update={"stage": "state_switched"})
            self._write_state()
        self._finish_recovered_workspace(pending)
        state.pending_mutation = None
        state.events.append(CheckpointAuditEvent(
            action="mutation_recovered", timestamp=_now(), checkpoint_sha=pending.target_commit_sha,
            details={"mutation_id": pending.mutation_id, "kind": pending.kind},
        ))
        self._write_state()

    def _recover_merge(self, pending: CheckpointPendingMutation) -> None:
        state = self._require_state()
        if pending.old_head_sha is None:
            raise RuntimeError("merge intent缺少旧活动HEAD")
        active = self._branch(pending.old_branch_id)
        actual = self._resolve_ref(active.ref)
        if actual == pending.old_head_sha:
            self._update_ref(active.ref, pending.target_commit_sha, pending.old_head_sha)
        elif actual != pending.target_commit_sha:
            raise RuntimeError("merge ref结果无法与durable intent对应")
        sequence = pending.merge_checkpoint_sequence
        if sequence is None or not pending.archived_branch_id or not pending.merge_attempt_id:
            raise RuntimeError("merge intent缺少恢复身份")
        checkpoint_ref = _checkpoint_ref(sequence)
        if self._resolve_ref(checkpoint_ref) is None:
            self._update_ref(checkpoint_ref, pending.target_commit_sha, None)
        record = next((item for item in state.commit_records if item.commit_sha == pending.target_commit_sha), None)
        archived = self._branch(pending.archived_branch_id)
        if record is None:
            record = CheckpointRecord(
                sequence=sequence, commit_sha=pending.target_commit_sha,
                tree_sha=pending.target_tree_sha, ref=checkpoint_ref,
                source="checkpoint_dream_merge", created_at=pending.started_at,
                changes=tuple(self._changes(pending.old_head_sha, pending.target_commit_sha)),
                metadata={"archived_branch_id": archived.branch_id,
                          "merge_attempt_id": pending.merge_attempt_id},
                branch_id=active.branch_id, parent_checkpoint_sha=pending.old_head_sha,
                merge_parent_sha=archived.head_commit_sha,
            )
            state.commit_records.append(record)
            state.restore_points.append(_restore_point(record))
            state.next_sequence = max(state.next_sequence, sequence + 1)
        self._replace_branch(active.model_copy(update={"head_commit_sha": pending.target_commit_sha}))
        state.pending_mutation = pending.model_copy(update={"stage": "state_switched"})
        self._write_state()
        self._finish_recovered_workspace(pending)
        merged_at = _now()
        self._replace_branch(archived.model_copy(update={
            "status": "merged", "merge_state": None, "merged_at": merged_at,
            "gc_after": merged_at + timedelta(days=self.merged_ref_retention_days),
        }))
        assessment = CheckpointValueAssessment.model_validate(
            pending.value_assessment, strict=False,
        )
        if not any(item.attempt_id == pending.merge_attempt_id for item in state.merge_attempts):
            self._append_merge_attempt(
                archived, "merged", "recovered_validated_merge", assessment,
                candidate_commit=pending.target_commit_sha,
                attempt_id=pending.merge_attempt_id,
                expected_active_head=pending.old_head_sha,
            )
        state.pending_mutation = None
        state.events.append(CheckpointAuditEvent(
            action="mutation_recovered", timestamp=merged_at,
            checkpoint_sha=pending.target_commit_sha,
            details={"mutation_id": pending.mutation_id, "kind": pending.kind},
        ))
        self._trim_restore_points()
        self._write_state()

    def _finish_recovered_workspace(self, pending: CheckpointPendingMutation) -> None:
        if pending.old_head_sha is None:
            raise RuntimeError("workspace恢复intent缺少旧活动HEAD")
        workspace_tree = self._workspace_tree()
        old_tree = self._record(pending.old_head_sha).tree_sha
        if workspace_tree == old_tree:
            self._restore(pending.target_commit_sha)
        elif workspace_tree != pending.target_tree_sha:
            raise RuntimeError(
                "checkpoint mutation恢复时发现workspace含外部变化；已保留intent且不会覆盖",
            )
        self._verify_workspace_tree(pending.target_tree_sha)

    def _merge_tree(self, base: str, ours: str, theirs: str) -> tuple[str, list[str]]:
        with tempfile.TemporaryDirectory(prefix="yy-checkpoint-merge-") as value:
            index = Path(value) / "merge.index"
            environment = self._git_environment()
            environment["GIT_INDEX_FILE"] = str(index)
            result = subprocess.run(
                self._git_command(["read-tree", "-m", base, ours, theirs]),
                cwd=self.project_root, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False,
            )
            if result.returncode != 0:
                return "", [result.stderr.strip() or "read-tree failed"]
            conflicts_result = subprocess.run(
                self._git_command(["ls-files", "-u"]), cwd=self.project_root,
                env=environment, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
            )
            conflicts = sorted({line.split("\t", 1)[-1] for line in conflicts_result.stdout.splitlines() if line})
            if conflicts:
                return "", conflicts
            tree_result = subprocess.run(
                self._git_command(["write-tree"]), cwd=self.project_root, env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            if tree_result.returncode != 0:
                return "", [tree_result.stderr.strip() or "write-tree failed"]
            return tree_result.stdout.strip(), []

    def _append_merge_attempt(
        self,
        branch: CheckpointBranchRecord,
        outcome: str,
        reason: str,
        assessment: CheckpointValueAssessment | None,
        *,
        candidate_commit: str | None = None,
        attempt_id: str | None = None,
        expected_active_head: str | None = None,
    ) -> None:
        active = self.active_branch()
        now = _now()
        self._require_state().merge_attempts.append(CheckpointMergeAttempt(
            attempt_id=attempt_id or uuid4().hex,
            branch_id=branch.branch_id,
            expected_active_head=(
                expected_active_head or active.head_commit_sha
                or branch.fork_checkpoint_sha or _ZERO_SHA
            ),
            archived_head=branch.head_commit_sha or branch.fork_checkpoint_sha or _ZERO_SHA,
            fork_commit=branch.fork_checkpoint_sha or _ZERO_SHA,
            candidate_commit=candidate_commit,
            outcome=outcome,
            reason=reason[:4000] or "unspecified",
            value_assessment=assessment.model_dump(mode="json") if assessment else {},
            created_at=now,
            completed_at=now,
        ))

    def _trim_restore_points(self) -> None:
        state = self._require_state()
        protected = {self.active_branch().head_commit_sha}
        if state.pending_mutation is not None:
            protected.add(state.pending_mutation.target_commit_sha)
        while len(state.restore_points) > self.limit:
            candidate = next(
                (item for item in sorted(state.restore_points, key=lambda value: value.sequence)
                 if item.commit_sha not in protected),
                None,
            )
            if candidate is None:
                break
            state.restore_points.remove(candidate)
            state.events.append(CheckpointAuditEvent(
                action="evicted", timestamp=_now(), checkpoint_sha=candidate.commit_sha,
                details={"sequence": candidate.sequence, "limit": self.limit,
                         "kind": "restore_point_evicted"},
            ))
            # 先持久化“恢复点已淘汰”的控制面事实，再删除非权威ref。强杀只会留下
            # 一个可安全清理的额外ref，不会让索引指向已经不存在的恢复点。
            self._write_state()
            self._delete_ref(candidate.ref, missing_ok=True)

    def _cleanup_evicted_restore_refs(self) -> None:
        """幂等清理eviction提交后、ref删除前崩溃留下的非权威checkpoint ref。"""
        expected = {item.ref for item in self._require_state().restore_points}
        output = self._git(["for-each-ref", "--format=%(refname)", "refs/yy/checkpoints"])
        for ref in output.splitlines():
            if ref and ref not in expected:
                self._delete_ref(ref, missing_ok=True)

    def _migrate_v1(self, raw: dict[str, Any]) -> CheckpointState:
        """保持旧commit SHA不变，仅把平面索引投影成一条legacy分支。"""
        backup = self._require_state_path().with_name("index.v1.backup.json")
        if not backup.exists():
            _write_bytes_durable(backup, (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode())
        old_records = [
            CheckpointRecord.model_validate(item, strict=False)
            for item in raw.get("checkpoints", [])
        ]
        records: list[CheckpointRecord] = []
        parent: str | None = None
        for item in old_records:
            migrated = item.model_copy(update={
                "branch_id": "legacy-main", "parent_checkpoint_sha": parent,
            })
            records.append(migrated)
            parent = migrated.commit_sha
        head = records[-1].commit_sha if records else None
        branch = CheckpointBranchRecord(
            branch_id="legacy-main", ref="refs/yy/branches/legacy-main",
            status="active", head_commit_sha=head,
            created_at=records[0].created_at if records else _now(),
        )
        if head:
            actual = self._resolve_ref(branch.ref)
            if actual is None:
                self._update_ref(branch.ref, head, None)
            elif actual != head:
                raise ValueError("legacy checkpoint branch ref冲突")
        state = CheckpointState(
            session_id=str(raw["session_id"]), workspace_root=str(self.project_root),
            active_branch_id=branch.branch_id,
            next_sequence=int(raw.get("next_sequence", len(records) + 1)),
            next_branch_sequence=2,
            commit_records=records,
            restore_points=[_restore_point(item) for item in records],
            branches=[branch],
            events=[
                CheckpointAuditEvent.model_validate(item, strict=False)
                for item in raw.get("events", [])
            ],
        )
        self._state = state
        self._trim_restore_points()
        self._write_state()
        return state

    def _configure_repository(self) -> None:
        info = self._require_git_dir() / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text("\n".join(_EXCLUDES) + "\n", encoding="utf-8")
        self._git(["config", "core.logAllRefUpdates", "false"])

    def _validate_refs(self) -> None:
        state = self._require_state()
        for record in state.commit_records:
            if self._git_result(["cat-file", "-e", f"{record.commit_sha}^{{commit}}"]).returncode != 0:
                raise ValueError(f"checkpoint对象缺失：{record.commit_sha}")
        for branch in state.branches:
            if branch.head_commit_sha is None or branch.ref_deleted_at is not None:
                continue
            actual = self._resolve_ref(branch.ref)
            if actual != branch.head_commit_sha:
                raise ValueError(f"checkpoint branch ref不一致：{branch.branch_id}")
        if len(state.restore_points) > self.limit:
            self._trim_restore_points()
            self._write_state()

    def _workspace_tree(self) -> str:
        self._git(["add", "-A", "--", "."])
        return self._git(["write-tree"]).strip()

    def _verify_workspace_tree(self, expected: str) -> None:
        actual = self._workspace_tree()
        if actual != expected:
            raise RuntimeError(f"workspace tree验证失败：expected={expected} actual={actual}")
        self._git(["read-tree", "--reset", expected])

    def _changes(self, previous_sha: str | None, current_sha: str) -> list[str]:
        if previous_sha is None:
            output = self._git(["ls-tree", "-r", "--name-only", current_sha])
            return [f"A\t{line}" for line in output.splitlines() if line]
        output = self._git(["diff", "--name-status", previous_sha, current_sha])
        return [line for line in output.splitlines() if line]

    def _restore(self, commit_sha: str) -> None:
        untracked = self._git_bytes(["ls-files", "--others", "--exclude-standard", "-z"])
        for raw in untracked.split(b"\0"):
            if not raw:
                continue
            path = _safe_restore_path(self.project_root, raw.decode("utf-8", errors="strict"))
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        self._load_index(commit_sha, update_worktree=True)
        self._remove_empty_directories()

    def _load_index(self, commit_sha: str, *, update_worktree: bool = False) -> None:
        arguments = ["read-tree", "--reset"]
        if update_worktree:
            arguments.append("-u")
        arguments.append(commit_sha)
        self._git(arguments)

    def _remove_empty_directories(self) -> None:
        protected = {".git", ".yy", ".venv", ".agents", ".codex"}
        directories = sorted(
            (path for path in self.project_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts), reverse=True,
        )
        for path in directories:
            try:
                relative = path.relative_to(self.project_root)
                if any(part in protected for part in relative.parts):
                    continue
                path.rmdir()
            except OSError:
                continue

    def _record(self, commit_sha: str | None) -> CheckpointRecord:
        if commit_sha is None:
            raise KeyError("checkpoint commit为空")
        found = next((item for item in self._require_state().commit_records if item.commit_sha == commit_sha), None)
        if found is None:
            raise KeyError(f"checkpoint commit未记录：{commit_sha}")
        return found

    def _branch(self, branch_id: str) -> CheckpointBranchRecord:
        found = next((item for item in self._require_state().branches if item.branch_id == branch_id), None)
        if found is None:
            raise KeyError(f"checkpoint branch不存在：{branch_id}")
        return found

    def _replace_branch(self, updated: CheckpointBranchRecord) -> None:
        state = self._require_state()
        state.branches[:] = [updated if item.branch_id == updated.branch_id else item for item in state.branches]

    def _resolve_ref(self, ref: str) -> str | None:
        result = self._git_result(["rev-parse", "--verify", ref])
        return result.stdout.strip() if result.returncode == 0 else None

    def _update_ref(self, ref: str, new_sha: str, old_sha: str | None) -> None:
        result = self._git_result(["update-ref", ref, new_sha, old_sha or _ZERO_SHA])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"checkpoint ref CAS失败：{ref}")

    def _delete_ref(self, ref: str, *, missing_ok: bool = False) -> None:
        if missing_ok and self._resolve_ref(ref) is None:
            return
        result = self._git_result(["update-ref", "-d", ref])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"删除checkpoint ref失败：{ref}")

    def _prune(self) -> None:
        self._git(["reflog", "expire", "--expire=now", "--all"])
        self._git(["prune", "--expire=now"])

    def _write_state(self) -> None:
        payload = self._require_state().model_dump_json(indent=2).encode("utf-8") + b"\n"
        _write_bytes_durable(self._require_state_path(), payload)

    def _checkpoint_base(self) -> Path:
        base = self.state_root / ".yy" / "sandbox" / "checkpoints"
        return base / _workspace_key(self.project_root) if self.state_root != self.project_root else base

    def _run_plain(self, arguments: list[str]) -> str:
        result = subprocess.run(
            arguments, cwd=self.project_root, env=SensitiveEnvSanitizer.subprocess_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"命令执行失败：{arguments[0]}")
        return result.stdout

    def _git(self, arguments: list[str], *, input_text: str | None = None, identity: bool = False) -> str:
        result = self._git_result(arguments, input_text=input_text, identity=identity)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"checkpoint Git命令失败：{' '.join(arguments)}")
        return result.stdout

    def _git_bytes(self, arguments: list[str]) -> bytes:
        result = subprocess.run(
            self._git_command(arguments), cwd=self.project_root, env=self._git_environment(),
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
        return result.stdout

    def _git_result(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        identity: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = self._git_environment()
        if identity:
            environment.update({
                "GIT_AUTHOR_NAME": "Yuan Ye Sandbox",
                "GIT_AUTHOR_EMAIL": "sandbox@local.invalid",
                "GIT_COMMITTER_NAME": "Yuan Ye Sandbox",
                "GIT_COMMITTER_EMAIL": "sandbox@local.invalid",
            })
        return subprocess.run(
            self._git_command(arguments), cwd=self.project_root, env=environment,
            input=input_text, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )

    def _git_command(self, arguments: list[str]) -> list[str]:
        return ["git", f"--git-dir={self._require_git_dir()}", f"--work-tree={self.project_root}", *arguments]

    def _git_environment(self) -> dict[str, str]:
        return SensitiveEnvSanitizer.subprocess_env({
            "GIT_INDEX_FILE": str(self._require_index_path()),
            "GIT_CONFIG_NOSYSTEM": "1",
        })

    def _require_state(self) -> CheckpointState:
        if self._state is None:
            raise RuntimeError("CheckpointStore 尚未打开 Session")
        return self._state

    def _require_git_dir(self) -> Path:
        if self._git_dir is None:
            raise RuntimeError("CheckpointStore 尚未打开 Session")
        return self._git_dir

    def _require_index_path(self) -> Path:
        if self._index_path is None:
            raise RuntimeError("CheckpointStore 尚未打开 Session")
        return self._index_path

    def _require_state_path(self) -> Path:
        if self._state_path is None:
            raise RuntimeError("CheckpointStore 尚未打开 Session")
        return self._state_path


def _restore_point(record: CheckpointRecord) -> CheckpointRestorePoint:
    return CheckpointRestorePoint(
        sequence=record.sequence, commit_sha=record.commit_sha,
        ref=record.ref, created_at=record.created_at,
    )


def _checkpoint_ref(sequence: int) -> str:
    return f"refs/yy/checkpoints/{sequence:08d}"


def _branch_number(branch_id: str) -> int:
    try:
        return int(branch_id.rsplit("-", 1)[-1])
    except ValueError:
        return 1


def _now() -> datetime:
    return datetime.now().astimezone()


def _write_bytes_durable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _redact_text(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in ("authorization", "api_key", "apikey", "password", "cookie", "secret")):
        lines = []
        for line in value.splitlines():
            low = line.lower()
            lines.append("[REDACTED SENSITIVE PATH]" if any(
                marker in low for marker in ("authorization", "api_key", "apikey", "password", "cookie", "secret")
            ) else line)
        return "\n".join(lines)
    return value


def _safe_session_id(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        raise ValueError("Session ID只能包含字母、数字、下划线和连字符")
    return value


def _workspace_key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _safe_restore_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise PermissionError("checkpoint包含越界路径")
    path = (root / Path(*pure.parts)).resolve()
    if root != path and root not in path.parents:
        raise PermissionError("checkpoint恢复路径超出workspace")
    return path
