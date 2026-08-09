"""Whole-home restore with external fence and write-ahead filesystem journal."""

from __future__ import annotations

import hashlib
import os
import shutil
import json
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .archive import EncryptedBackupArchive
from .catalog import AgentHomeDurabilityCatalog
from .control import (
    ExternalControlLock,
    RestoreJournal,
    create_restore_fence,
    external_control_root,
    read_restore_fence,
    remove_restore_fence,
)
from .models import RestoreFence, RestorePlan, RestoreState
from .service import BackupService
from .security import SensitiveEnvSanitizer


class RestoreConfirmationError(RuntimeError):
    pass


class RestoreRecoveryRequired(RuntimeError):
    pass


class RestoreService:
    def __init__(self, agent_root: Path, backup_service: BackupService) -> None:
        self.agent_root = agent_root.resolve()
        self.home = self.agent_root / ".yy"
        self.backups = backup_service
        self.control_root = external_control_root(self.agent_root)

    def plan(
        self,
        archive: Path,
        passphrase: str,
        path_mappings: dict[str, str] | None = None,
    ) -> RestorePlan:
        manifest = EncryptedBackupArchive.inspect_manifest(archive, passphrase)
        mappings = path_mappings or {}
        logical = manifest.agent_home_logical_size
        current = _directory_size(self.home)
        # staging + current-home rescue archive estimate + migration scratch + .partial
        baseline = logical + current + archive.stat().st_size + max(logical // 4, 256 * 1024 * 1024)
        margin = max(int(baseline * 0.10), 1024 * 1024 * 1024)
        peak = baseline + margin
        available = shutil.disk_usage(self.agent_root).free
        return RestorePlan(
            backup_id=manifest.backup_id,
            archive_path=archive.resolve(),
            created_at=manifest.created_at,
            agent_version=manifest.agent_version,
            schema_versions=manifest.schema_versions,
            archive_size=archive.stat().st_size,
            logical_size=logical,
            estimated_peak_bytes=peak,
            available_bytes=available,
            external_dependencies=tuple(
                self._mapped_dependency(item, mappings) for item in manifest.external_dependencies
            ),
            path_changes=mappings,
        )

    async def restore(
        self,
        archive: Path,
        passphrase: str,
        *,
        confirmation: str,
        non_interactive: bool = False,
        path_mappings: dict[str, str] | None = None,
    ) -> str:
        if read_restore_fence(self.agent_root) is not None:
            raise RestoreRecoveryRequired("存在未完成Restore，请先执行 backup recover/rollback")
        mappings = path_mappings or {}
        plan = self.plan(archive, passphrase, mappings)
        expected = plan.backup_id[:8]
        if confirmation != expected:
            raise RestoreConfirmationError(f"必须输入备份短ID {expected} 确认")
        if non_interactive and not passphrase:
            raise RestoreConfirmationError("non-interactive Restore必须提供Secret Provider")
        if plan.available_bytes < plan.estimated_peak_bytes:
            raise OSError(
                f"磁盘空间不足：需要约 {plan.estimated_peak_bytes} 字节，现有 {plan.available_bytes} 字节",
            )

        restore_lock = ExternalControlLock(self.control_root / "locks" / "restore.lock")
        restore_lock.acquire()
        # Hold the same byte-range lock as the Gateway while publishing the
        # Fence. This closes the check/start race: either the running Gateway
        # owns the lock and Restore refuses, or the Fence becomes durable
        # before any new Gateway can acquire the instance lock.
        gateway_lock = ExternalControlLock(
            self.control_root / "control" / "gateway" / "instance.lock",
        )
        try:
            gateway_lock.acquire()
        except Exception:
            restore_lock.close()
            raise RuntimeError("Gateway仍在运行；Restore必须先停止Gateway")
        restore_id = uuid4().hex
        staging = self.agent_root / f".yy.restore.{restore_id}"
        rollback = self.agent_root / f".yy.rollback.{restore_id}"
        if staging.anchor != self.home.anchor or rollback.anchor != self.home.anchor:
            raise OSError("Restore staging、rollback与Agent Home必须位于同一volume")
        journal_path = self.control_root / "restores" / f"{restore_id}.jsonl"
        journal = RestoreJournal(journal_path)
        fence = RestoreFence(
            restore_id=restore_id,
            journal_path=journal_path,
            backup_format_version=1,
            target_agent_root_identity=_path_identity(self.agent_root),
            created_at=datetime.now().astimezone(),
        )
        try:
            create_restore_fence(self.agent_root, fence)
        except Exception:
            gateway_lock.close()
            restore_lock.close()
            raise
        gateway_lock.close()
        journal.append("restore_state", {"state": RestoreState.PREPARING.value})
        try:
            EncryptedBackupArchive.extract(archive, passphrase, staging)
            journal.append("restore_state", {
                "state": RestoreState.PREPARED.value,
                "staging_fingerprint": _tree_identity(staging),
            })
            rescue = await self.backups.create(passphrase=passphrase, kind="rescue")
            if not self.backups.verify(rescue.path, passphrase).valid:
                raise RuntimeError("救援备份验证失败")
            journal.append("rescue_backup", {
                "path": str(rescue.path), "backup_id": rescue.backup_id,
            })
            journal.append("restore_state", {"state": RestoreState.GATEWAY_STOPPED.value})
            if self.home.exists():
                self._rename_action(journal, "rename-old-home", self.home, rollback)
            journal.append("restore_state", {"state": RestoreState.OLD_HOME_RENAMED.value})
            self._rename_action(journal, "install-new-home", staging, self.home)
            journal.append("restore_state", {"state": RestoreState.NEW_HOME_INSTALLED.value})
            self._restore_harness_candidates(mappings)
            self._validate_installed_home()
            journal.append("restore_state", {"state": RestoreState.MIGRATED.value})
            journal.append("restore_state", {"state": RestoreState.HEALTH_VERIFIED.value})
            journal.append("restore_state", {"state": RestoreState.COMMITTED.value})
            remove_restore_fence(self.agent_root, restore_id)
            shutil.rmtree(rollback, ignore_errors=True)
            restore_lock.close()
            return restore_id
        except Exception as exc:
            journal.append("restore_failure", {"message": str(exc) or type(exc).__name__})
            try:
                await self._rollback(journal, restore_id, staging, rollback)
            finally:
                restore_lock.close()
            raise

    async def recover_interrupted_restore(self) -> RestoreState:
        restore_lock = ExternalControlLock(self.control_root / "locks" / "restore.lock")
        restore_lock.acquire()
        try:
            return await self._recover_interrupted_restore_locked()
        finally:
            restore_lock.close()

    async def _recover_interrupted_restore_locked(self) -> RestoreState:
        fence = read_restore_fence(self.agent_root)
        if fence is None:
            raise RuntimeError("没有未完成Restore")
        journal = RestoreJournal(fence.journal_path)
        records = journal.records()
        if not records:
            raise RestoreRecoveryRequired("Fence存在但Journal为空")
        states = [
            item.payload.get("state") for item in records if item.record_type == "restore_state"
        ]
        if states and states[-1] == RestoreState.COMMITTED.value:
            remove_restore_fence(self.agent_root, fence.restore_id)
            return RestoreState.COMMITTED
        intents = {item.action_id: item for item in records if item.record_type == "action_intent"}
        commits = {item.action_id for item in records if item.record_type == "action_committed"}
        for action_id, intent in intents.items():
            if action_id in commits:
                continue
            source = Path(str(intent.payload["source"]))
            target = Path(str(intent.payload["target"]))
            expected = str(intent.payload["source_fingerprint"])
            if target.exists() and _tree_identity(target) == expected and not source.exists():
                journal.commit_action(action_id or "", {
                    "reconciled": True, "target_fingerprint": expected,
                })
            elif source.exists() and _tree_identity(source) == expected and not target.exists():
                self._rename_action(journal, f"{action_id}-retry", source, target)
            else:
                journal.append("restore_state", {"state": RestoreState.RECOVERY_REQUIRED.value})
                raise RestoreRecoveryRequired("无法证明未提交文件系统动作的结果")
        return RestoreState.RECOVERY_REQUIRED

    async def rollback(self) -> RestoreState:
        restore_lock = ExternalControlLock(self.control_root / "locks" / "restore.lock")
        restore_lock.acquire()
        try:
            return await self._rollback_locked()
        finally:
            restore_lock.close()

    async def _rollback_locked(self) -> RestoreState:
        fence = read_restore_fence(self.agent_root)
        if fence is None:
            raise RuntimeError("没有未完成Restore")
        journal = RestoreJournal(fence.journal_path)
        rollback = self.agent_root / f".yy.rollback.{fence.restore_id}"
        staging = self.agent_root / f".yy.restore.{fence.restore_id}"
        await self._rollback(journal, fence.restore_id, staging, rollback)
        return RestoreState.ROLLED_BACK

    async def _rollback(
        self,
        journal: RestoreJournal,
        restore_id: str,
        staging: Path,
        rollback: Path,
    ) -> None:
        journal.append("restore_state", {"state": RestoreState.ROLLING_BACK.value})
        if rollback.exists():
            if self.home.exists():
                failed = self.agent_root / f".yy.failed.{restore_id}"
                self._rename_action(journal, "quarantine-failed-home", self.home, failed)
            self._rename_action(journal, "restore-old-home", rollback, self.home)
        shutil.rmtree(staging, ignore_errors=True)
        journal.append("restore_state", {"state": RestoreState.ROLLED_BACK.value})
        remove_restore_fence(self.agent_root, restore_id)

    @staticmethod
    def _rename_action(journal: RestoreJournal, action_id: str, source: Path, target: Path) -> None:
        fingerprint = _tree_identity(source)
        journal.begin_action(action_id, {
            "source": str(source),
            "target": str(target),
            "source_fingerprint": fingerprint,
            "target_must_not_exist": True,
        })
        if target.exists():
            raise FileExistsError(target)
        os.replace(source, target)
        journal.commit_action(action_id, {
            "target_fingerprint": _tree_identity(target),
        })

    def _validate_installed_home(self) -> None:
        required = ["settings.local.json", ".initialized.json"]
        missing = [name for name in required if not (self.home / name).is_file()]
        if missing:
            raise RuntimeError(f"Restore后Agent Home缺少关键文件：{missing}")

    def _mapped_dependency(self, dependency, mappings: dict[str, str]):
        if not dependency.path:
            return dependency
        selected = Path(mappings.get(dependency.path, dependency.path)).expanduser().resolve()
        mapped = str(selected) != str(Path(dependency.path).expanduser().resolve())
        if not (selected / ".git").exists():
            status = "offline"
        elif _repository_identity(selected) != dependency.repository_identity:
            status = "incompatible"
        elif not all(_git_commit_exists(selected, commit) for commit in dependency.required_commits):
            status = "incompatible"
        else:
            status = "mapped" if mapped else "available"
        return dependency.model_copy(update={"path": str(selected), "status": status})

    def _restore_harness_candidates(self, mappings: dict[str, str]) -> None:
        candidates = self.home / "harness-evolution" / "candidates"
        if not candidates.is_dir():
            return
        worktrees = self.home / "harness-evolution" / "worktrees" / "restored"
        for metadata_path in sorted(candidates.glob("*/candidate.json")):
            status: dict[str, object]
            try:
                value = json.loads(metadata_path.read_text(encoding="utf-8"))
                original = str(value["source_repo_path"])
                repository = Path(mappings.get(original, original)).expanduser().resolve()
                expected_identity = str(value["repository_identity"])
                required = tuple(str(item) for item in value.get("required_commits", []))
                mapped = str(repository) != str(Path(original).expanduser().resolve())
                if not (repository / ".git").exists():
                    status = {"status": "offline", "repository": str(repository)}
                elif _repository_identity(repository) != expected_identity:
                    status = {"status": "incompatible", "repository": str(repository),
                              "reason": "repository_identity_mismatch"}
                elif not all(_git_commit_exists(repository, commit) for commit in required):
                    status = {"status": "incompatible", "repository": str(repository),
                              "reason": "required_commit_missing"}
                else:
                    session_id = str(value["code_session_id"])
                    target = worktrees / session_id
                    branch = f"yy-restore/{session_id[:12]}"
                    candidate_commit = str(value["candidate_commit"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _git(repository, "worktree", "add", "-b", branch, str(target), candidate_commit)
                    tracked = metadata_path.parent / str(value["tracked_diff"])
                    staged = metadata_path.parent / str(value["staged_diff"])
                    if tracked.stat().st_size:
                        _git(target, "apply", "--binary", str(tracked))
                    if staged.stat().st_size:
                        _git(target, "apply", "--index", "--binary", str(staged))
                    self._extract_untracked(
                        metadata_path.parent / str(value["untracked_file_bundle"]), target,
                    )
                    status = {
                        "status": "mapped" if mapped else "available",
                        "repository": str(repository),
                        "worktree": str(target),
                        "branch": branch,
                    }
            except Exception as exc:
                status = {"status": "incompatible", "reason": str(exc) or type(exc).__name__}
            (metadata_path.parent / "restore-status.json").write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )

    @staticmethod
    def _extract_untracked(bundle_path: Path, target: Path) -> None:
        with zipfile.ZipFile(bundle_path) as bundle:
            for info in bundle.infolist():
                relative = AgentHomeDurabilityCatalog.validate_member_name(info.filename)
                destination = (target / Path(*relative.parts)).resolve()
                if target.resolve() not in destination.parents or info.is_dir():
                    raise RuntimeError(f"Harness untracked bundle路径非法：{info.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as incoming, destination.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)


def _path_identity(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()


def _tree_identity(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode())
        if item.is_file() and not item.is_symlink():
            digest.update(str(item.stat().st_size).encode())
            with item.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _directory_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        env=SensitiveEnvSanitizer.subprocess_env({
            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
        }),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:] or "Git命令失败")
    return result


def _repository_identity(path: Path) -> str:
    result = _git(path, "remote", "get-url", "origin", check=False)
    raw = result.stdout.strip() or os.path.normcase(str(path.resolve()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _git_commit_exists(path: Path, commit: str) -> bool:
    if not commit:
        return False
    return _git(path, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0


__all__ = [
    "RestoreConfirmationError",
    "RestoreRecoveryRequired",
    "RestoreService",
]
