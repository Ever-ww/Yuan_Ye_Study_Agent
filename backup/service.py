"""Consistent encrypted Agent Home backup orchestration."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import shutil
import sqlite3
import subprocess
import tempfile
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from tzlocal import get_localzone_name

from .archive import ArchiveSource, EncryptedBackupArchive, build_sources, sha256_file
from .catalog import AgentHomeDurabilityCatalog
from .maintenance import AgentHomeMaintenanceCoordinator
from .models import (
    BackupFileRecord,
    BackupManifest,
    BackupRecord,
    BackupVerificationResult,
    DurabilityClass,
    ExternalDependency,
)
from .security import SensitiveEnvSanitizer


SecretProvider = Callable[[], str | None]
_SQLITE_HEADER = b"SQLite format 3\x00"
_REQUIRED_SQLITE_PATHS = {
    "gateway/gateway.sqlite3",
    "reference/reference.sqlite3",
}


class BackupService:
    def __init__(
        self,
        agent_root: Path,
        *,
        coordinator: AgentHomeMaintenanceCoordinator | None = None,
        catalog: AgentHomeDurabilityCatalog | None = None,
        secret_provider: SecretProvider | None = None,
        backup_directory: Path | None = None,
        source_root: Path | None = None,
        retention_daily: int = 7,
        retention_weekly: int = 4,
        retention_monthly: int = 12,
        min_free_space_bytes: int | None = None,
        max_storage_bytes: int | None = None,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.home = self.agent_root / ".yy"
        self.control_root = self.agent_root / ".yy-backups"
        self.backup_directory = (
            backup_directory.resolve()
            if backup_directory is not None
            else self.control_root / "archives"
        )
        self.coordinator = coordinator
        self.catalog = catalog or AgentHomeDurabilityCatalog()
        self.secret_provider = secret_provider
        self.source_root = source_root.resolve() if source_root else None
        self.retention_daily = retention_daily
        self.retention_weekly = retention_weekly
        self.retention_monthly = retention_monthly
        self.min_free_space_bytes = min_free_space_bytes
        self.max_storage_bytes = max_storage_bytes
        self.index_path = self.control_root / "index.json"

    async def create(
        self,
        *,
        passphrase: str | None = None,
        output: Path | None = None,
        kind: str = "manual",
        drain_timeout_seconds: float = 300,
    ) -> BackupRecord:
        selected = passphrase or (self.secret_provider() if self.secret_provider else None)
        if not selected:
            raise ValueError("没有可用的加密口令；Backup绝不降级为明文")
        if not self.home.is_dir():
            raise FileNotFoundError(f"Agent Home尚未初始化：{self.home}")
        self._ensure_backup_space()
        frozen = False
        epoch = 1
        if self.coordinator is not None:
            snapshot = await self.coordinator.freeze("backup", drain_timeout_seconds)
            epoch = snapshot.maintenance_epoch
            frozen = True
        maintenance = self.control_root / "maintenance" / str(epoch)
        maintenance.mkdir(parents=True, exist_ok=True)
        try:
            sources, logical_size = self._consistent_sources(maintenance)
            backup_id = uuid4().hex
            created_at = datetime.now().astimezone()
            manifest = BackupManifest(
                backup_id=backup_id,
                created_at=created_at,
                kind=kind,
                agent_version=_agent_version(),
                schema_versions=self._schema_versions(sources),
                maintenance_epoch=epoch,
                source_platform=f"{platform.system()} {platform.release()} {platform.machine()}",
                source_timezone=get_localzone_name(),
                agent_home_logical_size=logical_size,
                files=tuple(item.record for item in sources),
                external_dependencies=self._external_dependencies(maintenance),
                skill_manifest_hashes=self._skill_hashes(),
                harness_snapshots=tuple(
                    item.archive_path for item in sources
                    if item.archive_path.startswith("harness-evolution/candidates/")
                ),
            )
            target = output or self.backup_directory / (
                f"{created_at:%Y-%m-%d_%H%M%S}_{backup_id[:12]}.yybackup"
            )
            path = EncryptedBackupArchive.write(target, selected, manifest, sources)
            verification = self.verify(path, selected)
            if not verification.valid:
                path.unlink(missing_ok=True)
                raise RuntimeError("Backup发布后验证失败：" + "; ".join(verification.errors))
            record = BackupRecord(
                backup_id=backup_id,
                path=path,
                kind=kind,
                verification_status="verified",
                size_bytes=path.stat().st_size,
                created_at=created_at,
                retention_class=kind,
            )
            self._record_backup(record)
            if kind == "automatic":
                self.apply_retention()
            return record
        finally:
            if frozen and self.coordinator is not None:
                await self.coordinator.resume(epoch)
            shutil.rmtree(maintenance, ignore_errors=True)

    def verify(self, archive: Path, passphrase: str) -> BackupVerificationResult:
        errors: list[str] = []
        manifest: BackupManifest | None = None
        with tempfile.TemporaryDirectory(prefix="yy-backup-verify-") as directory:
            destination = Path(directory) / "home"
            try:
                manifest = EncryptedBackupArchive.extract(archive, passphrase, destination)
            except Exception as exc:
                return BackupVerificationResult(
                    valid=False,
                    gcm_authenticated=False,
                    manifest_valid=False,
                    file_hashes_valid=False,
                    sqlite_valid=False,
                    indexes_valid=False,
                    checkpoint_store_valid=False,
                    errors=(str(exc) or type(exc).__name__,),
                )
            sqlite_valid = True
            for file in manifest.files:
                path = destination / Path(*PurePosixPath(file.path).parts)
                if not _is_sqlite_source(path, file.path):
                    continue
                connection: sqlite3.Connection | None = None
                try:
                    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
                    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("quick_check失败")
                    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if foreign:
                        raise RuntimeError(f"foreign_key_check发现{len(foreign)}项")
                except Exception as exc:
                    sqlite_valid = False
                    errors.append(f"SQLite验证失败 {file.path}: {exc}")
                finally:
                    if connection is not None:
                        connection.close()
            return BackupVerificationResult(
                backup_id=manifest.backup_id,
                valid=not errors and sqlite_valid,
                gcm_authenticated=True,
                manifest_valid=True,
                file_hashes_valid=True,
                sqlite_valid=sqlite_valid,
                indexes_valid=True,
                checkpoint_store_valid=True,
                external_dependency_status={
                    item.dependency_id: item.status for item in manifest.external_dependencies
                },
                errors=tuple(errors),
            )

    def list(self) -> tuple[BackupRecord, ...]:
        indexed = self._read_index()
        records: list[BackupRecord] = []
        for value in indexed.get("backups", []):
            try:
                record = BackupRecord.model_validate(value)
            except Exception:
                continue
            if record.path.is_file():
                records.append(record)
        known = {record.path.resolve() for record in records}
        if not self.backup_directory.is_dir():
            return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))
        for path in sorted(self.backup_directory.glob("*.yybackup"), reverse=True):
            if path.resolve() in known:
                continue
            records.append(BackupRecord(
                backup_id=_id_from_name(path),
                path=path,
                kind="manual",
                verification_status="pending",
                size_bytes=path.stat().st_size,
                created_at=datetime.fromtimestamp(path.stat().st_mtime).astimezone(),
                retention_class="unknown",
            ))
        return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))

    def prune_automatic(self, keep: Iterable[Path] = ()) -> tuple[Path, ...]:
        records = list(self.list())
        automatic = [item for item in records if item.kind == "automatic"]
        keep_set = {item.resolve() for item in keep}
        keep_set.update(self._gfs_keep(automatic))
        if automatic:
            keep_set.add(max(automatic, key=lambda item: item.created_at).path.resolve())
        removed: list[Path] = []
        for record in automatic:
            if record.path.resolve() not in keep_set:
                record.path.unlink(missing_ok=True)
                removed.append(record.path)
        remaining = [item for item in records if item.path not in removed]
        if self.max_storage_bytes is not None:
            total = sum(item.size_bytes for item in remaining)
            candidates = sorted(
                (item for item in remaining if item.kind == "automatic" and item.path.resolve() not in keep_set),
                key=lambda item: item.created_at,
            )
            for record in candidates:
                if total <= self.max_storage_bytes:
                    break
                record.path.unlink(missing_ok=True)
                total -= record.size_bytes
                removed.append(record.path)
            remaining = [item for item in remaining if item.path not in removed]
        if removed:
            self._write_index(remaining)
        return tuple(removed)

    def apply_retention(self) -> tuple[Path, ...]:
        return self.prune_automatic()

    def _consistent_sources(self, maintenance: Path) -> tuple[tuple[ArchiveSource, ...], int]:
        sources, _ = build_sources(self.home, self.catalog)
        sqlite_dir = maintenance / "sqlite"
        candidate_dir = maintenance / "participants" / "harness"
        selected: list[ArchiveSource] = []
        logical_size = 0
        for item in sources:
            source = item.source
            record = item.record
            if _is_sqlite_source(source, item.archive_path):
                snapshot = sqlite_dir / Path(*PurePosixPath(item.archive_path).parts)
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                self._snapshot_sqlite(source, snapshot)
                record = record.model_copy(update={
                    "size": snapshot.stat().st_size,
                    "sha256": sha256_file(snapshot),
                })
                source = snapshot
            selected.append(ArchiveSource(source, item.archive_path, record))
            logical_size += record.size
        if candidate_dir.is_dir():
            for source in sorted(candidate_dir.rglob("*")):
                if not source.is_file() or source.is_symlink():
                    continue
                relative = source.relative_to(candidate_dir).as_posix()
                archive_path = f"harness-evolution/candidates/{relative}"
                record = BackupFileRecord(
                    path=archive_path,
                    size=source.stat().st_size,
                    sha256=sha256_file(source),
                    durability=DurabilityClass.CANONICAL,
                )
                selected.append(ArchiveSource(source, archive_path, record))
                logical_size += record.size
        return tuple(selected), logical_size

    @staticmethod
    def _snapshot_sqlite(source: Path, target: Path) -> None:
        incoming = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        outgoing: sqlite3.Connection | None = None
        try:
            if incoming.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"SQLite quick_check失败：{source}")
            if incoming.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError(f"SQLite foreign_key_check失败：{source}")
            outgoing = sqlite3.connect(target)
            incoming.backup(outgoing)
            outgoing.commit()
        finally:
            if outgoing is not None:
                outgoing.close()
            incoming.close()
        check = sqlite3.connect(target)
        try:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError(f"SQLite Backup API快照无效：{source}")
        finally:
            check.close()

    def _external_dependencies(self, maintenance: Path) -> tuple[ExternalDependency, ...]:
        values: list[ExternalDependency] = []
        if self.source_root is not None:
            values.append(ExternalDependency(
                dependency_id="yuan-ye-source",
                kind="git_repository",
                path=str(self.source_root),
                repository_identity=_repository_identity(self.source_root),
                status="available" if self.source_root.is_dir() else "offline",
            ))
        harness = maintenance / "participants" / "harness"
        if harness.is_dir():
            for candidate in sorted(harness.glob("*/candidate.json")):
                try:
                    value = json.loads(candidate.read_text(encoding="utf-8"))
                    values.append(ExternalDependency(
                        dependency_id=f"harness:{value['code_session_id']}",
                        kind="git_repository",
                        path=str(value["source_repo_path"]),
                        repository_identity=str(value["repository_identity"]),
                        required_commits=tuple(str(item) for item in value.get("required_commits", [])),
                        status="available",
                    ))
                except (OSError, KeyError, TypeError, json.JSONDecodeError):
                    raise RuntimeError(f"Harness Candidate Snapshot元数据损坏：{candidate}")
        return tuple(values)

    def _ensure_backup_space(self) -> None:
        logical = sum(
            path.stat().st_size for path in self.home.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        required = logical + max(logical // 10, 256 * 1024 * 1024)
        available = shutil.disk_usage(self.agent_root).free
        minimum = self.min_free_space_bytes or 0
        if available - required < minimum:
            self.apply_retention()
            available = shutil.disk_usage(self.agent_root).free
        if available - required < minimum:
            raise OSError(
                f"Backup空间不足：需要保留{minimum}字节空闲且预计使用{required}字节",
            )

    def _record_backup(self, record: BackupRecord) -> None:
        records = [
            item for item in self.list()
            if item.backup_id != record.backup_id and item.path.resolve() != record.path.resolve()
        ]
        records.append(record)
        self._write_index(records)

    def _read_index(self) -> dict[str, object]:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_index(self, records: Iterable[BackupRecord]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".partial")
        payload = {
            "version": 1,
            "backups": [item.model_dump(mode="json") for item in sorted(
                records, key=lambda value: value.created_at,
            )],
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.index_path)

    def _gfs_keep(self, records: list[BackupRecord]) -> set[Path]:
        keep: set[Path] = set()
        newest = sorted(records, key=lambda item: item.created_at, reverse=True)

        def select(key, limit: int) -> None:
            seen: set[object] = set()
            for record in newest:
                bucket = key(record.created_at)
                if bucket in seen:
                    continue
                seen.add(bucket)
                if len(seen) <= limit:
                    keep.add(record.path.resolve())

        select(lambda value: value.date(), self.retention_daily)
        select(lambda value: (value.isocalendar().year, value.isocalendar().week), self.retention_weekly)
        select(lambda value: (value.year, value.month), self.retention_monthly)
        return keep

    def _skill_hashes(self) -> dict[str, str]:
        if self.source_root is None or not (self.source_root / "skills").is_dir():
            return {}
        result: dict[str, str] = {}
        for skill in sorted((self.source_root / "skills").iterdir()):
            main = skill / "SKILL.md"
            if skill.is_dir() and main.is_file():
                result[skill.name] = sha256_file(main)
        return result

    @staticmethod
    def _schema_versions(sources: tuple[ArchiveSource, ...]) -> dict[str, int | str]:
        return {
            PurePosixPath(item.archive_path).name: "sqlite"
            for item in sources
            if _is_sqlite_source(item.source, item.archive_path)
        }


def _is_sqlite_source(path: Path, archive_path: str) -> bool:
    """Recognize actual SQLite data without misclassifying unknown canonical files."""
    normalized = PurePosixPath(archive_path).as_posix().lstrip("./")
    if normalized in _REQUIRED_SQLITE_PATHS:
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _agent_version() -> str:
    try:
        return importlib.metadata.version("yy-agent")
    except importlib.metadata.PackageNotFoundError:
        return "development"


def _repository_identity(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        env=SensitiveEnvSanitizer.subprocess_env({"GIT_TERMINAL_PROMPT": "0"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    raw = result.stdout.strip() or os.path.normcase(str(path.resolve()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _id_from_name(path: Path) -> str:
    stem = path.stem
    tail = stem.rsplit("_", 1)[-1]
    return tail if len(tail) >= 8 else hashlib.sha256(str(path).encode()).hexdigest()[:12]


__all__ = ["BackupService", "SecretProvider"]
