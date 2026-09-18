from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from pathlib import PurePosixPath
from unittest.mock import patch

from backup import (
    AgentHomeDurabilityCatalog,
    AgentHomeWriteGate,
    BackupService,
    BackupSecret,
    EncryptedBackupArchive,
    MaintenanceBlockedError,
    RestoreJournal,
    RestoreService,
    SystemManagedBackupKeyStore,
    read_manifest,
)
from backup.control import create_restore_fence, remove_restore_fence
from backup.control import ExternalControlLock
from backup.archive import ArchiveHeader, ArchiveSource, MAGIC, build_sources
from backup.models import (
    BackupFileRecord,
    BackupManifest,
    BackupOperation,
    BackupOperationPhase,
    DurabilityClass,
    MaintenanceSnapshot,
    MaintenanceState,
    RestoreFence,
)
from backup.archive import sha256_file
from backup.snapshot import write_manifest
from backup.scheduler import BackupScheduler
from Agent import load_runtime_config
from gateway.application import GatewayApplication


class BackupTests(unittest.TestCase):
    def test_catalog_paths_are_relative_to_dot_yy_root(self) -> None:
        catalog = AgentHomeDurabilityCatalog()
        self.assertEqual(
            catalog.classify(PurePosixPath("uv-cache/wheel.bin")),
            DurabilityClass.TRANSIENT,
        )
        self.assertEqual(
            catalog.classify(PurePosixPath("gateway/gateway.sqlite3-shm")),
            DurabilityClass.TRANSIENT,
        )
        self.assertEqual(
            catalog.classify(PurePosixPath("memory/index.sqlite3")),
            DurabilityClass.REBUILDABLE,
        )

    def test_space_estimate_ignores_transient_file_that_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            transient = home / "gateway.sqlite3-shm"
            transient.write_bytes(b"temporary")
            service = BackupService(root)
            original = Path.stat
            calls = 0

            def flaky_stat(path: Path, *args, **kwargs):
                nonlocal calls
                if path == transient:
                    calls += 1
                    if calls == 1:
                        transient.unlink()
                        raise FileNotFoundError(transient)
                return original(path, *args, **kwargs)

            with patch.object(Path, "stat", flaky_stat):
                service._ensure_backup_space()

    def test_scheduler_status_prefers_newer_verified_manual_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".yy").mkdir()
            service = BackupService(root)
            scheduler = BackupScheduler(
                service,
                AgentHomeWriteGate(),
                enabled=True,
                schedule="0 4 * * *",
                timezone="local",
                drain_timeout_seconds=30,
            )
            scheduler.state_path.parent.mkdir(parents=True, exist_ok=True)
            scheduler.state_path.write_text(json.dumps({
                "version": 1,
                "initialized_at": "2020-01-01T00:00:00+00:00",
                "last_successful_backup": None,
                "last_attempt_at": "2020-01-01T00:00:00+00:00",
                "last_status": "backup_failed",
                "last_error": "old failure",
                "last_path": None,
            }), encoding="utf-8")
            record = asyncio.run(service.create(passphrase="secret"))

            status = scheduler.status()

            self.assertEqual(status["last_status"], "backup_completed")
            self.assertIsNone(status["last_error"])
            self.assertEqual(status["last_path"], str(record.path))

            asyncio.run(scheduler.record_manual_success(record.created_at, record.path))
            persisted = json.loads(scheduler.state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["last_status"], "backup_completed")
            self.assertIsNone(persisted["last_error"])

    def test_storage_status_summarizes_active_operation_without_frozen_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / ".yy").mkdir()
            service = BackupService(root)
            now = datetime.now().astimezone()
            service.operation_store.create(BackupOperation(
                operation_id="active-operation",
                backup_id="active-backup",
                kind="manual",
                phase=BackupOperationPhase.PREPARING,
                staging_path=service.staging_root / "active-operation.partial",
                manifest_path=service.manifests_directory / "backup-active.manifest",
                encryption_mode="passphrase",
                manifest_json='{"large":"frozen recovery state"}',
                created_at=now,
                updated_at=now,
            ))

            active = service.storage_status()["active_operation"]

            self.assertIsInstance(active, dict)
            self.assertEqual(active["operation_id"], "active-operation")
            self.assertNotIn("manifest_json", active)

    def test_read_only_source_is_snapshotted_without_copying_read_only_attribute(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            source = home / "read-only.txt"
            source.write_text("immutable input", encoding="utf-8")
            os.chmod(source, stat.S_IREAD)
            try:
                service = BackupService(root)
                record = asyncio.run(service.create(passphrase="secret"))
                self.assertTrue(service.verify(record.path, "secret").valid)
            finally:
                os.chmod(source, stat.S_IREAD | stat.S_IWRITE)

    def test_snapshot_manifest_object_store_deduplicates_unchanged_content(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "settings.local.json").write_text("{}", encoding="utf-8")
            (home / "same-a.txt").write_text("same content", encoding="utf-8")
            (home / "same-b.txt").write_text("same content", encoding="utf-8")
            service = BackupService(root)

            first = asyncio.run(service.create(passphrase="secret"))
            first_objects = {
                path.name for path in service.object_store.objects_root.glob("*/*") if path.is_file()
            }
            second = asyncio.run(service.create(passphrase="secret"))
            second_objects = {
                path.name for path in service.object_store.objects_root.glob("*/*") if path.is_file()
            }

            self.assertEqual(first_objects, second_objects)
            self.assertNotEqual(first.path, second.path)
            self.assertRegex(first.path.name, r"^backup-\d{4}-\d{2}-\d{2}_")
            first_manifest = read_manifest(first.path)
            duplicate_ids = {
                item.object_id for item in first_manifest.files
                if item.path in {"same-a.txt", "same-b.txt"}
            }
            self.assertEqual(len(duplicate_ids), 1)

    def test_retention_removes_manifest_then_unreferenced_objects_after_27_days(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "only.txt").write_text("expires", encoding="utf-8")
            service = BackupService(root, retention_days=27)
            record = asyncio.run(service.create(passphrase="secret"))
            manifest = read_manifest(record.path).model_copy(update={
                "created_at": datetime.now().astimezone() - timedelta(days=28),
                "manifest_hash": None,
            })
            write_manifest(record.path, manifest)
            service.index_path.unlink(missing_ok=True)

            removed = service.apply_retention()

            self.assertEqual(removed, (record.path,))
            self.assertFalse(record.path.exists())
            self.assertEqual(
                tuple(path for path in service.object_store.objects_root.glob("*/*") if path.is_file()),
                (),
            )

    def test_gateway_resumes_before_object_storage_finishes(self) -> None:
        class Coordinator:
            def __init__(self) -> None:
                self.snapshot = MaintenanceSnapshot(
                    state=MaintenanceState.RUNNING, maintenance_epoch=0,
                )
                self.resumed = threading.Event()

            async def freeze(self, reason: str, timeout_seconds: float):
                del timeout_seconds
                self.snapshot = MaintenanceSnapshot(
                    state=MaintenanceState.QUIESCED,
                    maintenance_epoch=1,
                    reason=reason,
                    operation_id="lifecycle-operation",
                )
                return self.snapshot

            async def fail(self, reason: str) -> None:
                self.snapshot = self.snapshot.model_copy(update={
                    "state": MaintenanceState.FAILED, "failure_reason": reason,
                })

            async def resume(self, epoch: int) -> None:
                assert epoch == 1
                self.snapshot = self.snapshot.model_copy(update={"state": MaintenanceState.RUNNING})
                self.resumed.set()

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "payload.txt").write_text("payload", encoding="utf-8")
            coordinator = Coordinator()
            service = BackupService(root, coordinator=coordinator)  # type: ignore[arg-type]
            entered = threading.Event()
            release = threading.Event()
            original_put = service.object_store.put

            def slow_put(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test release was not signalled")
                return original_put(*args, **kwargs)

            service.object_store.put = slow_put  # type: ignore[method-assign]

            async def scenario() -> None:
                task = asyncio.create_task(service.create(passphrase="secret"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertTrue(coordinator.resumed.is_set())
                self.assertFalse(task.done())
                release.set()
                record = await task
                self.assertTrue(record.path.is_file())

            asyncio.run(scenario())

    def test_startup_reuses_frozen_members_instead_of_rescanning_agent_home(self) -> None:
        class FakeSystemKeyStore:
            key_id = "b" * 64

            def get_or_create(self) -> BackupSecret:
                return BackupSecret("recovery-secret", "os_managed", self.key_id)

            def get(self, key_id: str | None = None) -> BackupSecret | None:
                if key_id != self.key_id:
                    return None
                return self.get_or_create()

            def status(self) -> dict[str, object]:
                return {"supported": True, "key_available": True, "key_id": self.key_id}

        class Coordinator:
            def __init__(self) -> None:
                self.snapshot = MaintenanceSnapshot(
                    state=MaintenanceState.QUIESCED,
                    maintenance_epoch=3,
                    reason="backup",
                    operation_id="lifecycle-op",
                )
                self.resumed = False

            async def fail(self, reason: str) -> None:
                self.snapshot = self.snapshot.model_copy(update={
                    "state": MaintenanceState.FAILED, "failure_reason": reason,
                })

            async def resume(self, epoch: int) -> None:
                assert epoch == 3
                self.resumed = True
                self.snapshot = self.snapshot.model_copy(update={"state": MaintenanceState.RUNNING})

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            live = home / "fact.txt"
            live.write_text("new live value", encoding="utf-8")
            keys = FakeSystemKeyStore()
            service = BackupService(root, system_key_store=keys)  # type: ignore[arg-type]
            secret = keys.get_or_create()
            context = service.object_store.prepare(secret)
            operation_id = "operation-recovery"
            backup_id = "backup-recovery"
            staging = service.staging_root / f"{operation_id}.ready"
            (staging / "files").mkdir(parents=True)
            provisional = staging / "frozen.tmp"
            provisional.write_text("old frozen value", encoding="utf-8")
            frozen_hash = sha256_file(provisional)
            frozen = staging / "files" / frozen_hash
            provisional.replace(frozen)
            record = BackupFileRecord(
                path="fact.txt",
                size=frozen.stat().st_size,
                sha256=sha256_file(frozen),
                durability=DurabilityClass.CANONICAL,
                object_id=frozen_hash,
            )
            created = datetime.now().astimezone()
            manifest_path = service.manifests_directory / (
                f"backup-{created:%Y-%m-%d_%H%M%S_%f}_recovery.manifest"
            )
            manifest = BackupManifest(
                backup_id=backup_id,
                created_at=created,
                kind="automatic",
                backup_format_version=2,
                agent_version="test",
                maintenance_epoch=3,
                source_platform="test",
                source_timezone="UTC",
                agent_home_logical_size=record.size,
                files=(record,),
                encryption_mode="os_managed",
                key_id=keys.key_id,
                object_store_salt=context.metadata.salt,
                key_verifier=context.metadata.key_verifier,
            )
            now = datetime.now().astimezone()
            operation = service.operation_store.create(BackupOperation(
                operation_id=operation_id,
                backup_id=backup_id,
                kind="automatic",
                phase=BackupOperationPhase.PREPARING,
                staging_path=staging,
                manifest_path=manifest_path,
                encryption_mode="os_managed",
                key_id=keys.key_id,
                created_at=now,
                updated_at=now,
            ))
            service.operation_store.transition(
                operation_id,
                expected_revision=operation.revision,
                expected_phases=(BackupOperationPhase.PREPARING,),
                phase=BackupOperationPhase.SNAPSHOT_READY,
                maintenance_epoch=3,
                lifecycle_operation_id="lifecycle-op",
                manifest_json=manifest.model_dump_json(),
            )
            coordinator = Coordinator()
            recovered = BackupService(
                root, coordinator=coordinator, system_key_store=keys,  # type: ignore[arg-type]
            )

            async def scenario() -> None:
                await recovered.reconcile_startup()
                for _ in range(100):
                    if recovered.operation_store.active() is None:
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNone(recovered.operation_store.active())

            asyncio.run(scenario())
            self.assertTrue(coordinator.resumed)
            selected = keys.get_or_create()
            restored = root / "restored-recovery"
            recovered.extract_backup(manifest_path, selected, restored)
            self.assertEqual((restored / "fact.txt").read_text(encoding="utf-8"), "old frozen value")

    def test_system_managed_backup_uses_key_reference_and_restores_without_prompt(self) -> None:
        class FakeSystemKeyStore:
            key_id = "a" * 64

            def get_or_create(self) -> BackupSecret:
                return BackupSecret("generated-system-secret", "os_managed", self.key_id)

            def get(self, key_id: str | None = None) -> BackupSecret | None:
                if key_id != self.key_id:
                    return None
                return BackupSecret("generated-system-secret", "os_managed", self.key_id)

            def status(self) -> dict[str, object]:
                return {"supported": True, "key_available": True, "key_id": self.key_id}

        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "profile.txt").write_text("managed", encoding="utf-8")
            service = BackupService(root, system_key_store=FakeSystemKeyStore())  # type: ignore[arg-type]
            record = asyncio.run(service.create())
            manifest = read_manifest(record.path)
            self.assertEqual(record.encryption_mode, "os_managed")
            self.assertEqual(manifest.backup_format_version, 2)
            self.assertEqual(manifest.encryption_mode, "os_managed")
            self.assertEqual(manifest.key_id, "a" * 64)
            self.assertTrue(service.verify(record.path).valid)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_windows_system_key_is_stable_and_not_stored_in_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "profile.txt").write_text("dpapi", encoding="utf-8")
            first_store = SystemManagedBackupKeyStore(root)
            first = first_store.get_or_create()
            second = SystemManagedBackupKeyStore(root).get_or_create()
            self.assertEqual(first, second)
            raw = first_store.protected_path.read_bytes()
            self.assertNotIn(first.value.encode("utf-8"), raw)
            self.assertEqual(first.mode, "os_managed")
            service = BackupService(root)
            backup = asyncio.run(service.create())
            self.assertTrue(service.verify(backup.path).valid)

    def test_automatic_backup_inbox_is_silent_on_success_and_coalesces_failures(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            config = load_runtime_config(Path(value), dream_enabled=False)
            application = GatewayApplication(config)

            async def scenario() -> None:
                await application._record_backup_result(
                    "backup_skipped", {"message": "missing passphrase"},
                )
                await application._record_backup_result(
                    "backup_failed", {"message": "storage unavailable"},
                )
                unread = application.store.list_inbox(unread_only=True)
                self.assertEqual(len(unread), 1)
                self.assertEqual(unread[0].summary, "storage unavailable")

                await application._record_backup_result(
                    "backup_completed", {"path": "backup.yybackup"},
                )
                self.assertEqual(application.store.list_inbox(unread_only=True), [])
                self.assertEqual(len(application.store.list_inbox()), 3)

            asyncio.run(scenario())

    def test_streaming_archive_round_trip_and_wrong_password(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "unknown-empty-file").write_bytes(b"")
            (home / "profile.md").write_text("hello", encoding="utf-8")
            sources, size = build_sources(home, AgentHomeDurabilityCatalog())
            manifest = BackupManifest(
                backup_id="a" * 32,
                created_at=datetime.now().astimezone(),
                kind="manual",
                agent_version="test",
                maintenance_epoch=1,
                source_platform="test",
                source_timezone="UTC",
                agent_home_logical_size=size,
                files=tuple(item.record for item in sources),
            )
            archive = EncryptedBackupArchive.write(
                root / "test.yybackup", "secret", manifest, sources,
            )
            self.assertEqual(EncryptedBackupArchive.read_header(archive).key_mode, "passphrase")
            restored = root / "restored"
            selected = EncryptedBackupArchive.extract(archive, "secret", restored)
            self.assertEqual(selected.backup_id, manifest.backup_id)
            self.assertEqual((restored / "profile.md").read_text(encoding="utf-8"), "hello")
            self.assertTrue((restored / "unknown-empty-file").is_file())
            with self.assertRaises(Exception):
                EncryptedBackupArchive.inspect_manifest(archive, "wrong")

    def test_malicious_kdf_header_is_rejected_before_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "bad.yybackup"
            header = ArchiveHeader(
                scrypt_n=1 << 30,
                salt=base64.b64encode(b"s" * 16).decode(),
                nonce=base64.b64encode(b"n" * 12).decode(),
            ).model_dump_json().encode()
            path.write_bytes(MAGIC + len(header).to_bytes(4, "big") + header + b"x" * 32)
            with self.assertRaisesRegex(ValueError, "scrypt"):
                EncryptedBackupArchive.inspect_manifest(path, "secret")

    def test_backup_service_uses_clean_sqlite_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "settings.local.json").write_text("{}", encoding="utf-8")
            (home / ".initialized.json").write_text("{}", encoding="utf-8")
            database = home / "state.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE values_table(value TEXT)")
                connection.execute("INSERT INTO values_table VALUES ('ok')")
                connection.commit()
            finally:
                connection.close()
            service = BackupService(root)
            record = asyncio.run(service.create(passphrase="secret"))
            result = service.verify(record.path, "secret")
            self.assertTrue(result.valid, result.errors)
            self.assertTrue(result.sqlite_valid)

    def test_unknown_db_suffix_is_preserved_as_canonical_file(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "settings.local.json").write_text("{}", encoding="utf-8")
            (home / ".initialized.json").write_text("{}", encoding="utf-8")
            payload = b"this is not sqlite but must not be discarded"
            (home / "unknown.db").write_bytes(payload)
            service = BackupService(root)
            record = asyncio.run(service.create(passphrase="secret"))
            restored = root / "restored"
            service.extract_backup(
                record.path, BackupSecret("secret", "passphrase"), restored,
            )
            self.assertEqual((restored / "unknown.db").read_bytes(), payload)

    def test_write_gate_blocks_new_mutation_while_draining(self) -> None:
        async def scenario() -> None:
            gate = AgentHomeWriteGate()
            entered = asyncio.Event()
            release = asyncio.Event()

            async def writer() -> None:
                async with gate.operation("test", "one"):
                    entered.set()
                    await release.wait()

            task = asyncio.create_task(writer())
            await entered.wait()
            await gate.begin_draining(1)
            with self.assertRaises(MaintenanceBlockedError):
                async with gate.operation("test", "two"):
                    pass
            release.set()
            await task
            await gate.wait_for_idle(1)
            await gate.freeze(1)
            self.assertEqual(gate.state.value, "quiesced")

        asyncio.run(scenario())

    def test_restore_journal_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "restore.jsonl"
            journal = RestoreJournal(path)
            journal.append("restore_state", {"state": "preparing"})
            journal.begin_action("rename", {"source": "a", "target": "b"})
            self.assertEqual(len(journal.records()), 2)
            lines = path.read_text(encoding="utf-8").splitlines()
            payload = json.loads(lines[0])
            payload["payload"]["state"] = "committed"
            lines[0] = json.dumps(payload)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "哈希"):
                journal.records()

    def test_whole_home_restore_replaces_state_after_rescue_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "settings.local.json").write_text("{}", encoding="utf-8")
            (home / ".initialized.json").write_text("{}", encoding="utf-8")
            (home / "profile.txt").write_text("before", encoding="utf-8")
            service = BackupService(root)
            backup = asyncio.run(service.create(passphrase="secret"))
            (home / "profile.txt").write_text("after", encoding="utf-8")
            restore = RestoreService(root, service)
            restore_id = asyncio.run(restore.restore(
                backup.path,
                "secret",
                confirmation=backup.backup_id[:8],
            ))
            self.assertEqual(len(restore_id), 32)
            self.assertEqual((home / "profile.txt").read_text(encoding="utf-8"), "before")
            self.assertFalse((root / ".yy-backups" / "restores" / "active-fence.json").exists())
            from backup.lifecycle_store import LifecycleStore
            from backup.models import MaintenanceState
            self.assertEqual(LifecycleStore(root).read().state, MaintenanceState.RESTORING)

    def test_restore_refuses_while_gateway_instance_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / ".yy"
            home.mkdir()
            (home / "settings.local.json").write_text("{}", encoding="utf-8")
            (home / ".initialized.json").write_text("{}", encoding="utf-8")
            service = BackupService(root)
            backup = asyncio.run(service.create(passphrase="secret"))
            gateway_lock = ExternalControlLock(
                root / ".yy-backups" / "control" / "gateway" / "instance.lock",
            )
            gateway_lock.acquire()
            try:
                restore = RestoreService(root, service)
                with self.assertRaisesRegex(RuntimeError, "Gateway仍在运行"):
                    asyncio.run(restore.restore(
                        backup.path,
                        "secret",
                        confirmation=backup.backup_id[:8],
                    ))
            finally:
                gateway_lock.close()

    def test_restore_fence_precedes_runtime_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            journal = root / ".yy-backups" / "restores" / "r.jsonl"
            fence = RestoreFence(
                restore_id="r" * 32,
                journal_path=journal,
                backup_format_version=1,
                target_agent_root_identity="x" * 64,
                created_at=datetime.now().astimezone(),
            )
            create_restore_fence(root, fence)
            with self.assertRaisesRegex(RuntimeError, "正在恢复"):
                load_runtime_config(root)
            self.assertFalse((root / ".yy").exists())
            remove_restore_fence(root, fence.restore_id)


if __name__ == "__main__":
    unittest.main()
