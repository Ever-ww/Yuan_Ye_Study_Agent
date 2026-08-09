from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from backup import (
    AgentHomeDurabilityCatalog,
    AgentHomeWriteGate,
    BackupService,
    EncryptedBackupArchive,
    MaintenanceBlockedError,
    RestoreJournal,
    RestoreService,
)
from backup.control import create_restore_fence, remove_restore_fence
from backup.control import ExternalControlLock
from backup.archive import ArchiveHeader, ArchiveSource, MAGIC, build_sources
from backup.models import BackupManifest, RestoreFence
from Agent import load_runtime_config


class BackupTests(unittest.TestCase):
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
            EncryptedBackupArchive.extract(record.path, "secret", restored)
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
            self.assertEqual(gate.state.value, "frozen")

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
