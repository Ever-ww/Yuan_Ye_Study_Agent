"""Recoverable Snapshot Manifest/Object Store backup orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import os
import platform
import shutil
import sqlite3
import subprocess
import tempfile
import json
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import uuid4

from tzlocal import get_localzone_name

from .archive import ArchiveSource, EncryptedBackupArchive, build_sources, sha256_file
from .catalog import AgentHomeDurabilityCatalog
from .maintenance import AgentHomeMaintenanceCoordinator
from .models import (
    BackupFileRecord,
    BackupManifest,
    BackupOperation,
    BackupOperationPhase,
    BackupRecord,
    BackupVerificationResult,
    DurabilityClass,
    ExternalDependency,
)
from .object_store import BackupObjectStore
from .operations import BackupOperationConflict, BackupOperationStore
from .security import BackupSecret, SensitiveEnvSanitizer, SystemManagedBackupKeyStore
from .snapshot import materialize_snapshot, read_manifest, validate_sqlite_members, write_manifest


SecretProvider = Callable[[], BackupSecret | str | None]
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
        retention_days: int = 27,
        min_free_space_bytes: int | None = None,
        max_storage_bytes: int | None = None,
        system_key_store: SystemManagedBackupKeyStore | None = None,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.home = self.agent_root / ".yy"
        self.control_root = self.agent_root / ".yy-backups"
        self.backup_root = (
            backup_directory.resolve()
            if backup_directory is not None
            else self.control_root
        )
        self.manifests_directory = self.backup_root / "manifests"
        # Compatibility attribute used by API/CLI status.  New records point
        # to Snapshot Manifests, never monolithic archives.
        self.backup_directory = self.manifests_directory
        self.coordinator = coordinator
        self.catalog = catalog or AgentHomeDurabilityCatalog()
        self.secret_provider = secret_provider
        self.source_root = source_root.resolve() if source_root else None
        self.retention_days = retention_days
        self.min_free_space_bytes = min_free_space_bytes
        self.max_storage_bytes = max_storage_bytes
        # Rebuildable listing projection; Manifests and Operation evidence are
        # authoritative.  Keep it beside the backup control DB, not at root.
        self.index_path = self.control_root / "control" / "backup" / "index.json"
        self.system_key_store = system_key_store or SystemManagedBackupKeyStore(self.agent_root)
        self.operation_store = BackupOperationStore(self.agent_root)
        self.object_store = BackupObjectStore(self.backup_root)
        self.staging_root = self.control_root / "control" / "backup" / "staging"
        self._background_tasks: set[asyncio.Task[BackupRecord]] = set()

    async def create(
        self,
        *,
        passphrase: str | None = None,
        resolved_secret: BackupSecret | None = None,
        output: Path | None = None,
        kind: str = "manual",
        drain_timeout_seconds: float = 300,
    ) -> BackupRecord:
        if resolved_secret is not None and passphrase is not None:
            raise ValueError("passphrase 与 resolved_secret 不能同时提供")
        selected = resolved_secret or self._secret_for_create(passphrase)
        if not self.home.is_dir():
            raise FileNotFoundError(f"Agent Home尚未初始化：{self.home}")
        self._ensure_backup_space()
        context = self.object_store.prepare(selected)
        created_at = datetime.now().astimezone()
        backup_id = uuid4().hex
        operation_id = uuid4().hex
        manifest_path = self._manifest_target(output, created_at, backup_id)
        staging_partial = self.staging_root / f"{operation_id}.partial"
        staging_ready = self.staging_root / f"{operation_id}.ready"
        now = datetime.now().astimezone()
        operation = self.operation_store.create(BackupOperation(
            operation_id=operation_id,
            backup_id=backup_id,
            kind=kind,
            phase=BackupOperationPhase.PREPARING,
            staging_path=staging_ready,
            manifest_path=manifest_path,
            encryption_mode=selected.mode,
            key_id=selected.key_id,
            created_at=now,
            updated_at=now,
        ))
        epoch = 1
        maintenance: Path | None = None
        frozen = False
        try:
            if self.coordinator is not None:
                lifecycle = await self.coordinator.freeze("backup", drain_timeout_seconds)
                epoch = lifecycle.maintenance_epoch
                frozen = True
                maintenance = self.control_root / "maintenance" / str(epoch)
                operation = self.operation_store.transition(
                    operation_id,
                    expected_revision=operation.revision,
                    expected_phases=(BackupOperationPhase.PREPARING,),
                    phase=BackupOperationPhase.SNAPSHOTTING,
                    maintenance_epoch=epoch,
                    lifecycle_operation_id=lifecycle.operation_id,
                )
            else:
                maintenance = self.control_root / "maintenance" / str(epoch)
                operation = self.operation_store.transition(
                    operation_id,
                    expected_revision=operation.revision,
                    expected_phases=(BackupOperationPhase.PREPARING,),
                    phase=BackupOperationPhase.SNAPSHOTTING,
                    maintenance_epoch=epoch,
                )
            maintenance.mkdir(parents=True, exist_ok=True)
            sources, logical_size = self._stage_consistent_sources(maintenance, staging_partial)
            manifest = BackupManifest(
                backup_id=backup_id,
                created_at=created_at,
                kind=kind,
                agent_version=_agent_version(),
                backup_format_version=2,
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
                encryption_mode=selected.mode,
                key_id=selected.key_id,
                object_store_salt=context.metadata.salt,
                key_verifier=context.metadata.key_verifier,
            )
            self._write_staging_manifest(staging_partial / ".snapshot-manifest.json", manifest)
            staging_ready.parent.mkdir(parents=True, exist_ok=True)
            if staging_ready.exists():
                raise FileExistsError(staging_ready)
            os.replace(staging_partial, staging_ready)
            operation = self.operation_store.transition(
                operation_id,
                expected_revision=operation.revision,
                expected_phases=(BackupOperationPhase.SNAPSHOTTING,),
                phase=BackupOperationPhase.SNAPSHOT_READY,
                manifest_json=manifest.model_dump_json(),
            )
        except BaseException as exc:
            current = self.operation_store.get(operation_id)
            if current is not None and current.phase not in {
                BackupOperationPhase.COMPLETED,
                BackupOperationPhase.FAILED,
                BackupOperationPhase.RECOVERY_REQUIRED,
            }:
                self._terminal_transition(current, BackupOperationPhase.FAILED, exc)
            if frozen:
                await self._resume_after_snapshot(epoch, failed_reason=type(exc).__name__)
            shutil.rmtree(staging_partial, ignore_errors=True)
            shutil.rmtree(staging_ready, ignore_errors=True)
            raise
        else:
            if frozen:
                try:
                    await self._resume_after_snapshot(epoch)
                except BaseException as exc:
                    current = self.operation_store.get(operation_id)
                    if current is not None:
                        self._terminal_transition(
                            current, BackupOperationPhase.RECOVERY_REQUIRED, exc,
                        )
                    raise
            if maintenance is not None:
                shutil.rmtree(maintenance, ignore_errors=True)

        # Object compression/encryption and verification happen after the
        # short consistency window.  Cancellation of the caller cannot erase
        # the durable intent; startup reconciliation can resume this task.
        task = asyncio.create_task(
            self._finish_in_daemon(operation_id, selected),
            name=f"backup-object-store-{operation_id[:12]}",
        )
        self._track_background(task)
        return await asyncio.shield(task)

    def verify(self, archive: Path, passphrase: str | None = None) -> BackupVerificationResult:
        if archive.suffix == ".manifest":
            return self._verify_snapshot_manifest(archive, passphrase)
        errors: list[str] = []
        manifest: BackupManifest | None = None
        with tempfile.TemporaryDirectory(prefix="yy-backup-verify-") as directory:
            destination = Path(directory) / "home"
            try:
                selected = self.resolve_archive_secret(archive, passphrase)
                manifest = EncryptedBackupArchive.extract(archive, selected.value, destination)
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

    def inspect_manifest(self, backup: Path, passphrase: str | None = None) -> BackupManifest:
        if backup.suffix == ".manifest":
            manifest = read_manifest(backup)
            selected = self.resolve_archive_secret(backup, passphrase)
            self._store_for_manifest(backup).context_from_manifest(manifest, selected)
            return manifest
        selected = self.resolve_archive_secret(backup, passphrase)
        return EncryptedBackupArchive.inspect_manifest(backup, selected.value)

    def extract_backup(
        self,
        backup: Path,
        selected: BackupSecret,
        destination: Path,
    ) -> BackupManifest:
        if backup.suffix != ".manifest":
            return EncryptedBackupArchive.extract(backup, selected.value, destination)
        manifest = read_manifest(backup)
        store = self._store_for_manifest(backup)
        context = store.context_from_manifest(manifest, selected)
        return materialize_snapshot(backup, destination, store, context)

    def resolve_archive_secret(
        self,
        archive: Path,
        passphrase: str | None = None,
    ) -> BackupSecret:
        """Resolve an archive key without ever falling back to plaintext storage."""
        if archive.suffix == ".manifest":
            manifest = read_manifest(archive)
            if passphrase:
                selected = BackupSecret(passphrase, "passphrase")
            elif manifest.encryption_mode == "os_managed":
                selected = self.system_key_store.get(manifest.key_id)
                if selected is None:
                    raise ValueError("当前系统账户没有这份 Snapshot Manifest 的托管密钥")
            else:
                raise ValueError("该 Snapshot Manifest 使用手动口令，请输入创建时的口令")
            self._store_for_manifest(archive).context_from_manifest(manifest, selected)
            return selected
        header = EncryptedBackupArchive.read_header(archive)
        if passphrase:
            return BackupSecret(passphrase, "passphrase")
        if header.key_mode == "os_managed":
            selected = self.system_key_store.get(header.key_id)
            if selected is None:
                raise ValueError(
                    "当前系统账户没有这份备份的托管密钥；请回到原账户恢复，或使用手动口令备份迁移",
                )
            return selected
        raise ValueError("该备份使用手动口令，请输入创建它时使用的口令")

    def key_status(self) -> dict[str, object]:
        return self.system_key_store.status()

    def storage_status(self) -> dict[str, object]:
        active = self.operation_store.active()
        manifests = tuple(self.manifests_directory.glob("*.manifest")) if self.manifests_directory.is_dir() else ()
        objects = tuple(self.object_store.objects_root.glob("*/*")) if self.object_store.objects_root.is_dir() else ()
        active_summary = None
        if active is not None:
            active_summary = {
                "operation_id": active.operation_id,
                "backup_id": active.backup_id,
                "phase": active.phase.value,
                "attempt_count": active.attempt_count,
                "last_error_type": active.last_error_type,
                "last_error": active.last_error,
                "updated_at": active.updated_at,
            }
        return {
            "format": "snapshot-manifest-object-store-v2",
            "retention_days": self.retention_days,
            "manifest_count": len(manifests),
            "object_count": sum(path.is_file() for path in objects),
            "storage_bytes": self._backup_storage_size(),
            # The frozen manifest can contain one row per source file.  It is
            # recovery state, not health/status payload, so never expose it
            # through the status endpoint.
            "active_operation": active_summary,
        }

    def local_store_secret(self, fallback: BackupSecret) -> BackupSecret:
        """Select the key already bound to this local Object Store."""
        try:
            self.object_store.prepare(fallback)
            return fallback
        except ValueError:
            managed = self.system_key_store.get()
            if managed is None:
                raise
            self.object_store.prepare(managed)
            return managed

    def _secret_for_create(self, passphrase: str | None) -> BackupSecret:
        if passphrase:
            return BackupSecret(passphrase, "passphrase")
        if self.secret_provider is not None:
            supplied = self.secret_provider()
            if isinstance(supplied, BackupSecret):
                return supplied
            if isinstance(supplied, str) and supplied:
                return BackupSecret(supplied, "passphrase")
            raise ValueError(
                "没有可用的自动备份口令；请设置 YY_BACKUP_PASSPHRASE，或改用 os_managed 模式",
            )
        return self.system_key_store.get_or_create()

    async def reconcile_startup(self) -> None:
        """Resume or safely abandon the exact durable backup operation."""
        self.object_store.cleanup_interrupted_partials()
        if self.manifests_directory.is_dir():
            for partial in self.manifests_directory.glob(".*.partial"):
                partial.unlink(missing_ok=True)
        operation = self.operation_store.active()
        if operation is None:
            if self.staging_root.is_dir():
                for orphan in self.staging_root.iterdir():
                    if orphan.is_dir() and orphan.name.endswith((".partial", ".ready")):
                        shutil.rmtree(orphan, ignore_errors=True)
            if (
                self.coordinator is not None
                and self.coordinator.snapshot.reason == "backup"
                and self.coordinator.snapshot.state.value != "running"
            ):
                epoch = self.coordinator.snapshot.maintenance_epoch
                if self.coordinator.snapshot.state.value not in {"quiesced", "failed", "restoring"}:
                    await self.coordinator.fail("Interrupted legacy backup maintenance")
                await self.coordinator.resume(epoch)
            return
        if operation.phase in {
            BackupOperationPhase.PREPARING,
            BackupOperationPhase.SNAPSHOTTING,
        }:
            self._terminal_transition(
                operation,
                BackupOperationPhase.FAILED,
                RuntimeError("Gateway stopped before an immutable snapshot was published"),
            )
            shutil.rmtree(operation.staging_path, ignore_errors=True)
            await self._resume_interrupted_backup(operation)
            return

        await self._resume_interrupted_backup(operation)
        try:
            if operation.encryption_mode == "os_managed":
                selected = self.system_key_store.get(operation.key_id)
                if selected is None:
                    raise RuntimeError("System-managed backup key is unavailable")
            else:
                # Manual passphrases are never persisted.  An interrupted
                # manual backup can be retried explicitly, but cannot be
                # guessed by startup recovery.
                raise RuntimeError("Interrupted manual-passphrase backup requires an explicit retry")
        except Exception as exc:
            current = self.operation_store.get(operation.operation_id)
            if current is not None and current.phase not in {
                BackupOperationPhase.COMPLETED,
                BackupOperationPhase.FAILED,
                BackupOperationPhase.RECOVERY_REQUIRED,
            }:
                self._terminal_transition(current, BackupOperationPhase.FAILED, exc)
            shutil.rmtree(operation.staging_path, ignore_errors=True)
            return

        task = asyncio.create_task(
            self._finish_in_daemon(operation.operation_id, selected),
            name=f"backup-recovery-{operation.operation_id[:12]}",
        )
        self._track_background(task)

    async def _finish_in_daemon(
        self,
        operation_id: str,
        selected: BackupSecret,
    ) -> BackupRecord:
        """Run storage off-loop without making process exit wait for a pool thread.

        The worker may be cut off at any instruction when the Gateway process
        exits.  That is safe because every publish is atomic and the operation
        plus immutable staging are reconciled on the next startup.
        """
        loop = asyncio.get_running_loop()
        result: asyncio.Future[BackupRecord] = loop.create_future()

        def finish() -> None:
            try:
                value = self._finish_operation(operation_id, selected)
            except BaseException as exc:
                callback = lambda error=exc: (not result.done()) and result.set_exception(error)
            else:
                callback = lambda resolved=value: (not result.done()) and result.set_result(resolved)
            try:
                loop.call_soon_threadsafe(callback)
            except RuntimeError:
                # The event loop already closed.  Durable operation state is
                # now the only authority and startup reconciliation continues.
                pass

        threading.Thread(
            target=finish,
            name=f"yy-backup-{operation_id[:12]}",
            daemon=True,
        ).start()
        return await result

    async def _resume_interrupted_backup(self, operation: BackupOperation) -> None:
        if self.coordinator is None or operation.maintenance_epoch is None:
            return
        snapshot = self.coordinator.snapshot
        if snapshot.state.value == "running":
            return
        if snapshot.reason != "backup" or snapshot.maintenance_epoch != operation.maintenance_epoch:
            return
        if snapshot.state.value not in {"quiesced", "failed", "restoring"}:
            await self.coordinator.fail("Interrupted backup maintenance transition")
        await self.coordinator.resume(operation.maintenance_epoch)

    async def _resume_after_snapshot(
        self,
        epoch: int,
        *,
        failed_reason: str | None = None,
    ) -> None:
        if self.coordinator is None:
            return
        if failed_reason and self.coordinator.snapshot.state.value != "failed":
            await self.coordinator.fail(f"Backup snapshot failed: {failed_reason}")
        await self.coordinator.resume(epoch)

    def _finish_operation(self, operation_id: str, selected: BackupSecret) -> BackupRecord:
        operation = self.operation_store.get(operation_id)
        if operation is None:
            raise BackupOperationConflict(f"Unknown backup operation: {operation_id}")
        try:
            if operation.encryption_mode != selected.mode or operation.key_id != selected.key_id:
                raise ValueError("Backup operation key identity changed")
            if operation.phase == BackupOperationPhase.VERIFYING:
                return self._verify_and_complete(operation, selected)
            if operation.phase not in {
                BackupOperationPhase.SNAPSHOT_READY,
                BackupOperationPhase.STORING_OBJECTS,
            }:
                raise BackupOperationConflict(
                    f"Backup operation cannot be finished from {operation.phase.value}",
                )
            operation = self.operation_store.transition(
                operation_id,
                expected_revision=operation.revision,
                expected_phases=(operation.phase,),
                phase=BackupOperationPhase.STORING_OBJECTS,
                attempt_count=operation.attempt_count + 1,
                last_error_type=None,
                last_error=None,
            )
            if not operation.manifest_json:
                raise RuntimeError("Backup operation is missing its frozen member manifest")
            seed = BackupManifest.model_validate_json(operation.manifest_json, strict=True)
            context = self.object_store.prepare(selected)
            records: list[BackupFileRecord] = []
            for member in seed.files:
                AgentHomeDurabilityCatalog.validate_member_name(member.path)
                if member.object_id is None:
                    raise RuntimeError(f"Frozen member has no staging identity: {member.path}")
                source = (operation.staging_path / "files" / member.object_id).resolve()
                if operation.staging_path.resolve() not in source.parents or not source.is_file():
                    raise RuntimeError(f"Frozen backup member is missing: {member.path}")
                if source.stat().st_size != member.size or sha256_file(source) != member.sha256:
                    raise RuntimeError(f"Frozen backup member changed: {member.path}")
                object_id, stored_size, _ = self.object_store.put(
                    source, content_hash=member.sha256, context=context,
                )
                records.append(member.model_copy(update={
                    "object_id": object_id,
                    "stored_size": stored_size,
                }))
            manifest = seed.model_copy(update={"files": tuple(records)})
            finalized = write_manifest(operation.manifest_path, manifest)
            operation = self.operation_store.transition(
                operation_id,
                expected_revision=operation.revision,
                expected_phases=(BackupOperationPhase.STORING_OBJECTS,),
                phase=BackupOperationPhase.VERIFYING,
                manifest_json=finalized.model_dump_json(),
                manifest_hash=finalized.manifest_hash,
            )
            return self._verify_and_complete(operation, selected)
        except BaseException as exc:
            current = self.operation_store.get(operation_id)
            if current is not None and current.phase not in {
                BackupOperationPhase.COMPLETED,
                BackupOperationPhase.FAILED,
                BackupOperationPhase.RECOVERY_REQUIRED,
            }:
                phase = (
                    BackupOperationPhase.RECOVERY_REQUIRED
                    if isinstance(exc, (BackupOperationConflict, ValueError))
                    else BackupOperationPhase.FAILED
                )
                self._terminal_transition(current, phase, exc)
            raise

    def _verify_and_complete(
        self,
        operation: BackupOperation,
        selected: BackupSecret,
    ) -> BackupRecord:
        verification = self._verify_snapshot_manifest(
            operation.manifest_path,
            selected.value if selected.mode == "passphrase" else None,
        )
        if not verification.valid:
            raise RuntimeError("Snapshot Manifest verification failed: " + "; ".join(verification.errors))
        manifest = read_manifest(operation.manifest_path)
        size_bytes = sum({
            member.object_id: member.stored_size or 0 for member in manifest.files
        }.values()) + operation.manifest_path.stat().st_size
        record = BackupRecord(
            backup_id=manifest.backup_id,
            path=operation.manifest_path,
            kind=manifest.kind,
            verification_status="verified",
            size_bytes=size_bytes,
            created_at=manifest.created_at,
            retention_class=f"max-{self.retention_days}-days",
            encryption_mode=selected.mode,
            key_id=selected.key_id,
        )
        self._record_backup(record)
        # Keep the operation active until every filesystem side effect has
        # settled.  Startup/tests may use the absence of an active operation
        # as the completion fence, so publishing COMPLETED before cleanup
        # creates a real race with process shutdown or temporary-root removal.
        shutil.rmtree(operation.staging_path, ignore_errors=True)
        try:
            self.apply_retention()
        except Exception:
            # Retention is independently retryable maintenance.  It must not
            # turn an already verified immutable snapshot into a failed backup.
            pass
        self.operation_store.transition(
            operation.operation_id,
            expected_revision=operation.revision,
            expected_phases=(BackupOperationPhase.VERIFYING,),
            phase=BackupOperationPhase.COMPLETED,
        )
        return record

    def _verify_snapshot_manifest(
        self,
        manifest_path: Path,
        passphrase: str | None,
    ) -> BackupVerificationResult:
        errors: list[str] = []
        manifest: BackupManifest | None = None
        try:
            manifest = read_manifest(manifest_path)
            selected = self.resolve_archive_secret(manifest_path, passphrase)
            store = self._store_for_manifest(manifest_path)
            context = store.context_from_manifest(manifest, selected)
            with tempfile.TemporaryDirectory(prefix="yy-backup-sqlite-verify-") as directory:
                temp_root = Path(directory)
                for index, member in enumerate(manifest.files):
                    header = store.verify_object(member.object_id or "", context)
                    if header.content_sha256 != member.sha256 or header.original_size != member.size:
                        raise ValueError(f"Object metadata mismatch: {member.path}")
                    # Only SQLite members need materialization for structural
                    # checks.  Other objects are authenticated and hash-checked
                    # directly while streaming.
                    staged = temp_root / f"{index}.member"
                    source_hint = self._staging_member_if_available(
                        manifest.backup_id, member.object_id,
                    )
                    is_sqlite = source_hint is not None and _is_sqlite_source(source_hint, member.path)
                    if source_hint is None:
                        normalized = PurePosixPath(member.path).as_posix().lstrip("./")
                        is_sqlite = (
                            normalized in _REQUIRED_SQLITE_PATHS
                            or PurePosixPath(normalized).suffix.lower() in {".sqlite", ".sqlite3", ".db"}
                        )
                    if is_sqlite:
                        store.restore_object(member.object_id or "", staged, context)
                        if _is_sqlite_source(staged, member.path):
                            db = sqlite3.connect(f"file:{staged.as_posix()}?mode=ro", uri=True)
                            try:
                                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                                    raise RuntimeError(f"SQLite quick_check failed: {member.path}")
                                if db.execute("PRAGMA foreign_key_check").fetchall():
                                    raise RuntimeError(f"SQLite foreign_key_check failed: {member.path}")
                            finally:
                                db.close()
                        staged.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(str(exc) or type(exc).__name__)
        return BackupVerificationResult(
            backup_id=manifest.backup_id if manifest else None,
            valid=not errors,
            gcm_authenticated=not errors,
            manifest_valid=manifest is not None,
            file_hashes_valid=not errors,
            sqlite_valid=not errors,
            indexes_valid=not errors,
            checkpoint_store_valid=not errors,
            external_dependency_status={
                item.dependency_id: item.status for item in (manifest.external_dependencies if manifest else ())
            },
            errors=tuple(errors),
        )

    def _staging_member_if_available(
        self,
        backup_id: str,
        object_id: str | None,
    ) -> Path | None:
        active = self.operation_store.active()
        if active is None or active.backup_id != backup_id or object_id is None:
            return None
        candidate = active.staging_path / "files" / object_id
        return candidate if candidate.is_file() else None

    def _terminal_transition(
        self,
        operation: BackupOperation,
        phase: BackupOperationPhase,
        error: BaseException,
    ) -> BackupOperation:
        return self.operation_store.transition(
            operation.operation_id,
            expected_revision=operation.revision,
            expected_phases=(operation.phase,),
            phase=phase,
            last_error_type=type(error).__name__,
            last_error=(str(error) or type(error).__name__)[:2000],
        )

    def _track_background(self, task: asyncio.Task[BackupRecord]) -> None:
        self._background_tasks.add(task)

        def done(value: asyncio.Task[BackupRecord]) -> None:
            self._background_tasks.discard(value)
            if not value.cancelled():
                value.exception()

        task.add_done_callback(done)

    @staticmethod
    def _write_staging_manifest(path: Path, manifest: BackupManifest) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(manifest.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _manifest_target(self, output: Path | None, created_at: datetime, backup_id: str) -> Path:
        if output is not None:
            target = output.resolve()
            return target if target.suffix == ".manifest" else target.with_suffix(".manifest")
        return self.manifests_directory / (
            f"backup-{created_at:%Y-%m-%d_%H%M%S_%f}_{backup_id[:12]}.manifest"
        )

    def _store_for_manifest(self, manifest_path: Path) -> BackupObjectStore:
        parent = manifest_path.resolve().parent
        if parent.name == "manifests" and (parent.parent / "objects").exists():
            return BackupObjectStore(parent.parent)
        return self.object_store

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
        if not self.manifests_directory.is_dir():
            return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))
        for path in sorted(self.manifests_directory.glob("*.manifest"), reverse=True):
            if path.resolve() in known:
                continue
            try:
                manifest = read_manifest(path)
            except Exception:
                continue
            records.append(self._record_from_manifest(path, manifest, "pending"))
        return tuple(sorted(records, key=lambda item: item.created_at, reverse=True))

    def apply_retention(self) -> tuple[Path, ...]:
        """Apply the v2 hard age/space bound, then collect unreferenced objects."""
        if self.operation_store.active() is not None:
            return ()
        records = list(self.list())
        removed: list[Path] = []
        cutoff = datetime.now().astimezone() - timedelta(days=self.retention_days)
        for record in records:
            if record.created_at < cutoff:
                record.path.unlink(missing_ok=True)
                removed.append(record.path)
        remaining = [item for item in records if item.path not in removed]
        if self.max_storage_bytes is not None:
            for record in sorted(remaining, key=lambda item: item.created_at):
                self._collect_unreferenced_objects(remaining)
                total = self._backup_storage_size()
                if total <= self.max_storage_bytes:
                    break
                record.path.unlink(missing_ok=True)
                removed.append(record.path)
                remaining = [item for item in remaining if item.path != record.path]
        remaining = [item for item in remaining if item.path not in removed and item.path.is_file()]
        self._collect_unreferenced_objects(remaining)
        self._write_index(remaining)
        return tuple(removed)

    def _record_from_manifest(
        self,
        path: Path,
        manifest: BackupManifest,
        status: str,
    ) -> BackupRecord:
        size_bytes = path.stat().st_size + sum({
            member.object_id: member.stored_size or 0 for member in manifest.files
        }.values())
        return BackupRecord(
            backup_id=manifest.backup_id,
            path=path,
            kind=manifest.kind,
            verification_status=status,
            size_bytes=size_bytes,
            created_at=manifest.created_at,
            retention_class=f"max-{self.retention_days}-days",
            encryption_mode=manifest.encryption_mode or "passphrase",
            key_id=manifest.key_id,
        )

    def _collect_unreferenced_objects(self, records: Iterable[BackupRecord]) -> tuple[Path, ...]:
        referenced: set[str] = set()
        for record in records:
            if record.path.suffix != ".manifest" or not record.path.is_file():
                continue
            manifest = read_manifest(record.path)
            referenced.update(member.object_id for member in manifest.files if member.object_id)
        removed: list[Path] = []
        if self.object_store.objects_root.is_dir():
            for path in self.object_store.objects_root.glob("*/*"):
                if path.is_file() and path.name not in referenced:
                    path.unlink()
                    removed.append(path)
            for directory in self.object_store.objects_root.iterdir():
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        return tuple(removed)

    def _backup_storage_size(self) -> int:
        return sum(
            path.stat().st_size
            for root in (self.manifests_directory, self.object_store.objects_root)
            if root.is_dir()
            for path in root.rglob("*")
            if path.is_file()
        )

    def _stage_consistent_sources(
        self,
        maintenance: Path,
        staging: Path,
    ) -> tuple[tuple[ArchiveSource, ...], int]:
        if staging.exists():
            raise FileExistsError(staging)
        staging.mkdir(parents=True)
        frozen_sources, _ = self._consistent_sources(maintenance)
        staged: list[ArchiveSource] = []
        total = 0
        seen: set[str] = set()
        try:
            files_root = staging / "files"
            files_root.mkdir()
            for item in frozen_sources:
                if item.archive_path in seen:
                    raise RuntimeError(f"Duplicate backup member: {item.archive_path}")
                seen.add(item.archive_path)
                AgentHomeDurabilityCatalog.validate_member_name(item.archive_path)
                target = (files_root / item.record.sha256).resolve()
                if staging.resolve() not in target.parents:
                    raise RuntimeError(f"Backup member escapes staging: {item.archive_path}")
                if not target.exists():
                    self._copy_and_fsync(item.source, target)
                staged_hash = sha256_file(target)
                if target.stat().st_size != item.record.size or staged_hash != item.record.sha256:
                    raise RuntimeError(f"Backup source changed during snapshot: {item.archive_path}")
                record = item.record.model_copy(update={
                    "size": target.stat().st_size,
                    "sha256": staged_hash,
                    "object_id": item.record.sha256,
                    "stored_size": None,
                })
                staged.append(ArchiveSource(target, item.archive_path, record))
                total += record.size
            return tuple(staged), total
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _copy_and_fsync(source: Path, target: Path) -> None:
        """Copy bytes without inheriting a source read-only bit or ACL."""
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())

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
        logical = 0
        for path, _relative, _durability in self.catalog.iter_files(self.home):
            try:
                logical += path.stat().st_size
            except FileNotFoundError:
                # SQLite WAL/SHM and atomic temporary files may disappear
                # between directory enumeration and stat. They are not stable
                # snapshot members and must not abort admission estimation.
                continue
        # Peak usage includes one immutable staging copy plus newly created
        # objects.  Existing identical objects are reused, but admission must
        # remain safe for a completely changed home.
        required = (logical * 2) + max(logical // 10, 256 * 1024 * 1024)
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
