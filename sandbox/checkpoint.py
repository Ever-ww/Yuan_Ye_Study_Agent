"""使用独立 Git 对象库保存可物理淘汰的本地工作区快照。"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .models import CheckpointAuditEvent, CheckpointRecord, CheckpointState, RollbackResult


_EXCLUDES = (
    ".git/",
    ".yy/",
    ".env",
    ".env.*",
    ".venv/",
    ".agents/",
    ".codex/",
    "__pycache__/",
    "*.py[cod]",
)


class CheckpointStore:
    """把真实工作目录快照保存到 `.yy` 下的独立 Git 对象库。

    对象库不属于项目主仓库，也不会修改主仓库的 HEAD、分支或 index。
    每个 checkpoint 都是无父提交，因此删除 ref 后可以安全物理清理。
    """

    def __init__(
        self,
        project_root: Path,
        *,
        state_root: Path | None = None,
        limit: int = 17,
    ) -> None:
        if limit < 1:
            raise ValueError("checkpoint 上限必须大于等于 1")
        self.project_root = project_root.resolve()
        self.state_root = (state_root or project_root).resolve()
        self.limit = limit
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

    def open(self, session_id: str) -> None:
        """打开或创建指定 Session 的本地 checkpoint 对象库。"""
        safe_id = _safe_session_id(session_id)
        with self._lock:
            base = self.state_root / ".yy" / "sandbox" / "checkpoints"
            if self.state_root != self.project_root:
                base /= _workspace_key(self.project_root)
            directory = base / safe_id
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
                self._state = CheckpointState.model_validate_json(
                    self._state_path.read_text(encoding="utf-8"),
                )
                if self._state.session_id != safe_id:
                    raise ValueError("checkpoint 索引的 Session ID 不匹配")
                self._validate_refs()
            else:
                self._state = CheckpointState(session_id=safe_id)
                self._write_state()
            if self._state.checkpoints:
                self._load_index(self._state.checkpoints[-1].commit_sha)
            else:
                self._git(["read-tree", "--empty"])

    def create(
        self,
        source: str,
        metadata: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> CheckpointRecord | None:
        """创建无父快照；没有变化时默认不制造空 checkpoint。"""
        with self._lock:
            state = self._require_state()
            previous = state.checkpoints[-1] if state.checkpoints else None
            previous_sha = previous.commit_sha if previous else None
            self._git(["add", "-A", "--", "."])
            tree_sha = self._git(["write-tree"]).strip()
            if previous is not None and tree_sha == previous.tree_sha and not force:
                return None

            sequence = state.next_sequence
            created_at = datetime.now().astimezone()
            message = f"yy checkpoint {sequence}: {source}"
            commit_sha = self._git(
                ["commit-tree", tree_sha],
                input_text=message + "\n",
                identity=True,
            ).strip()
            ref = f"refs/yy/checkpoints/{sequence:08d}"
            self._git(["update-ref", ref, commit_sha])
            changes = self._changes(previous_sha, commit_sha)
            record = CheckpointRecord(
                sequence=sequence,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                ref=ref,
                source=source,
                created_at=created_at,
                changes=tuple(changes),
                metadata=dict(metadata or {}),
            )
            state.checkpoints.append(record)
            state.next_sequence += 1
            state.events.append(CheckpointAuditEvent(
                action="created",
                timestamp=created_at,
                checkpoint_sha=commit_sha,
                details={"source": source, "changes": len(changes)},
            ))
            self._trim()
            self._write_state()
            return record

    def rollback(self, steps: int) -> RollbackResult:
        """按 hard-reset 语义恢复到更早快照并删除其后的 checkpoint。"""
        if steps < 1:
            raise ValueError("回溯步数必须大于等于 1")
        with self._lock:
            state = self._require_state()
            if steps >= len(state.checkpoints):
                raise ValueError(
                    f"最多只能回溯 {max(len(state.checkpoints) - 1, 0)} 步",
                )
            target_index = len(state.checkpoints) - 1 - steps
            target = state.checkpoints[target_index]
            removed = tuple(state.checkpoints[target_index + 1 :])
            self._restore(target.commit_sha)
            for record in removed:
                self._delete_ref(record.ref)
            state.checkpoints[:] = state.checkpoints[: target_index + 1]
            state.events.append(CheckpointAuditEvent(
                action="rollback",
                timestamp=datetime.now().astimezone(),
                checkpoint_sha=target.commit_sha,
                details={"steps": steps, "removed": [record.commit_sha for record in removed]},
            ))
            self._write_state()
            self._prune()
            return RollbackResult(restored=target, removed=removed)

    def restore_current(self) -> CheckpointRecord:
        """丢弃未形成 checkpoint 的工作区变化，恢复当前最新快照。"""
        with self._lock:
            state = self._require_state()
            if not state.checkpoints:
                raise RuntimeError("当前 Session 尚无可恢复的 checkpoint")
            target = state.checkpoints[-1]
            self._restore(target.commit_sha)
            state.events.append(CheckpointAuditEvent(
                action="restored",
                timestamp=datetime.now().astimezone(),
                checkpoint_sha=target.commit_sha,
                details={"reason": "operation_failed"},
            ))
            self._write_state()
            return target

    def list(self) -> tuple[CheckpointRecord, ...]:
        """按创建顺序返回仍可回溯的 checkpoint。"""
        with self._lock:
            return tuple(self._require_state().checkpoints)

    def _configure_repository(self) -> None:
        git_dir = self._require_git_dir()
        info = git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text("\n".join(_EXCLUDES) + "\n", encoding="utf-8")
        self._git(["config", "core.logAllRefUpdates", "false"])

    def _validate_refs(self) -> None:
        state = self._require_state()
        valid: list[CheckpointRecord] = []
        for record in state.checkpoints:
            result = self._git_result(["cat-file", "-e", f"{record.commit_sha}^{{commit}}"])
            if result.returncode != 0:
                raise ValueError(f"checkpoint 对象缺失：{record.commit_sha}")
            valid.append(record)
        if len(valid) > self.limit:
            self._trim()
            self._write_state()

    def _changes(self, previous_sha: str | None, current_sha: str) -> list[str]:
        if previous_sha is None:
            output = self._git(["ls-tree", "-r", "--name-only", current_sha])
            return [f"A\t{line}" for line in output.splitlines() if line]
        output = self._git(["diff", "--name-status", previous_sha, current_sha])
        return [line for line in output.splitlines() if line]

    def _restore(self, commit_sha: str) -> None:
        # 先删除相对独立 index 而言的非忽略新文件，再用 read-tree -u 同步修改和删除。
        untracked = self._git_bytes(["ls-files", "--others", "--exclude-standard", "-z"])
        for raw in untracked.split(b"\0"):
            if not raw:
                continue
            relative = raw.decode("utf-8", errors="strict")
            path = _safe_restore_path(self.project_root, relative)
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
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for path in directories:
            try:
                relative = path.relative_to(self.project_root)
                if any(part in protected for part in relative.parts):
                    continue
                path.rmdir()
            except OSError:
                continue

    def _trim(self) -> None:
        state = self._require_state()
        removed: list[CheckpointRecord] = []
        while len(state.checkpoints) > self.limit:
            removed.append(state.checkpoints.pop(0))
        for record in removed:
            self._delete_ref(record.ref)
            state.events.append(CheckpointAuditEvent(
                action="evicted",
                timestamp=datetime.now().astimezone(),
                checkpoint_sha=record.commit_sha,
                details={"sequence": record.sequence, "limit": self.limit},
            ))
        if removed:
            self._prune()

    def _delete_ref(self, ref: str) -> None:
        result = self._git_result(["update-ref", "-d", ref])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"删除 checkpoint 引用失败：{ref}")

    def _prune(self) -> None:
        self._git(["reflog", "expire", "--expire=now", "--all"])
        self._git(["prune", "--expire=now"])

    def _write_state(self) -> None:
        state_path = self._require_state_path()
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(
            self._require_state().model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_path)

    def _run_plain(self, arguments: list[str]) -> str:
        result = subprocess.run(
            arguments,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"命令执行失败：{arguments[0]}")
        return result.stdout

    def _git(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        identity: bool = False,
    ) -> str:
        result = self._git_result(arguments, input_text=input_text, identity=identity)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"checkpoint Git 命令失败：{' '.join(arguments)}")
        return result.stdout

    def _git_bytes(self, arguments: list[str]) -> bytes:
        result = subprocess.run(
            self._git_command(arguments),
            cwd=self.project_root,
            env=self._git_environment(),
            capture_output=True,
            check=False,
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
            self._git_command(arguments),
            cwd=self.project_root,
            env=environment,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def _git_command(self, arguments: list[str]) -> list[str]:
        return [
            "git",
            f"--git-dir={self._require_git_dir()}",
            f"--work-tree={self.project_root}",
            *arguments,
        ]

    def _git_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(self._require_index_path())
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        return environment

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


def _safe_session_id(value: str) -> str:
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in value):
        raise ValueError("Session ID 只能包含字母、数字、下划线和连字符")
    return value


def _workspace_key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _safe_restore_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise PermissionError("checkpoint 包含越界路径")
    path = (root / Path(*pure.parts)).resolve()
    if root != path and root not in path.parents:
        raise PermissionError("checkpoint 恢复路径超出项目工作区")
    return path
