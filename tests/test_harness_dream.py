from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from dream.scheduler import DreamScheduler
from gateway.harness_dream import HarnessDreamChangeScanner
from gateway.state_controller import StateConflictError, StateController
from gateway.store import GatewayStore
from tools import (
    HarnessCapabilityTool,
    HarnessDreamTool,
    HarnessErrorTool,
    HarnessManualTool,
)


class HarnessDreamTests(unittest.TestCase):
    def test_scanner_consumes_only_proven_non_dream_merge_before_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = root / "source"
            agent = root / "agent"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "dream@example.test"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Dream Test"], cwd=source, check=True)
            (source / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=source, check=True,
                           stdout=subprocess.DEVNULL)
            base = self._git(source, "rev-parse", "HEAD")
            (source / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "capability"], cwd=source, check=True,
                           stdout=subprocess.DEVNULL)
            merged = self._git(source, "rev-parse", "HEAD")
            scanner = HarnessDreamChangeScanner(agent, source, "UTC")
            invocation_id = "a" * 32
            audit = scanner.audit_root / f"{invocation_id}.jsonl"
            audit.parent.mkdir(parents=True)
            occurred = "2026-08-15T08:00:00+00:00"
            event_id = hashlib.sha256(b"merge").hexdigest()
            records = (
                {"event": "invocation_started", "invocation_id": invocation_id,
                 "trigger": "capability"},
                {"event": "merge_intent", "base_commit": base,
                 "verified_commit": merged, "target_branch": "main",
                 "changed_files": ["sample.py"]},
                {"event": "merge_committed", "occurred_at": occurred,
                 "merge_event_id": event_id, "base_commit": base,
                 "verified_commit": merged, "merged_commit": merged,
                 "target_branch": "main", "changed_files": ["sample.py"]},
            )
            audit.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

            changeset = scanner.scan(
                date(2026, 8, 15), cutoff_at=datetime(2026, 8, 15, 9, tzinfo=timezone.utc),
            )
            self.assertEqual(changeset.merge_event_ids, (event_id,))
            self.assertEqual(changeset.merged_commits, (merged,))
            self.assertEqual(changeset.changed_files, ("sample.py",))
            too_early = scanner.scan(
                date(2026, 8, 15), cutoff_at=datetime(2026, 8, 15, 7, tzinfo=timezone.utc),
            )
            self.assertFalse(too_early.evidence)

    def test_no_changes_claim_creates_no_run_and_generation_cas_fences_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            database = Path(value) / "gateway.sqlite3"
            GatewayStore(Path(value))
            controller = StateController(database, gateway_epoch="epoch")
            empty = self._changeset("no-change", evidence=False)
            row, duplicate = controller.claim_harness_dream(
                empty, automatic_cycle=True, no_changes=True,
            )
            self.assertFalse(duplicate)
            self.assertEqual(row["status"], "no_changes")
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM harness_dream_generations").fetchone()[0],
                    0,
                )
            finally:
                connection.close()

            claimed, _ = controller.claim_harness_dream(
                self._changeset("work", evidence=True), automatic_cycle=True,
            )
            controller.start_harness_dream_generation(
                str(claimed["stable_key"]), run_id="run-1",
                expected_revision=int(claimed["revision"]),
            )
            with self.assertRaises(StateConflictError):
                controller.start_harness_dream_generation(
                    str(claimed["stable_key"]), run_id="run-2",
                    expected_revision=int(claimed["revision"]),
                )

    def test_harness_scheduler_runs_when_profile_dream_is_disabled(self) -> None:
        calls: list[date] = []
        config = SimpleNamespace(
            dream_enabled=False, harness_dream_enabled=True,
            dream_schedule="0 3 * * *", dream_timezone="UTC",
        )
        service = SimpleNamespace(config=config)

        async def check() -> None:
            scheduler = DreamScheduler(
                service, lambda: True, lambda result, automatic: asyncio.sleep(0),
                clock=lambda: datetime(2026, 8, 16, 4, tzinfo=timezone.utc),
                run_day=lambda selected: asyncio.sleep(0),
                run_harness_day=lambda selected: self._record_day(calls, selected),
            )
            result = await scheduler.tick()
            self.assertIsNone(result)

        asyncio.run(check())
        self.assertEqual(calls, [date(2026, 8, 15)])

    def test_checkpoint_dream_runs_once_with_due_profile_day(self) -> None:
        profile_calls: list[date] = []
        checkpoint_calls: list[date] = []
        config = SimpleNamespace(
            dream_enabled=True, harness_dream_enabled=False,
            dream_schedule="0 3 * * *", dream_timezone="UTC",
        )
        service = SimpleNamespace(
            config=config,
            status=lambda **kwargs: SimpleNamespace(last_completed_date=None),
            process_day=lambda selected: asyncio.sleep(0),
        )

        async def run_profile(selected: date):
            profile_calls.append(selected)
            return SimpleNamespace(status="noop", message="noop")

        async def check() -> None:
            scheduler = DreamScheduler(
                service, lambda: True, lambda result, automatic: asyncio.sleep(0),
                clock=lambda: datetime(2026, 8, 16, 4, tzinfo=timezone.utc),
                run_day=run_profile,
                run_checkpoint_day=lambda selected: self._record_day(checkpoint_calls, selected),
            )
            await scheduler.tick()

        asyncio.run(check())
        self.assertEqual(profile_calls, [date(2026, 8, 15)])
        self.assertEqual(checkpoint_calls, [date(2026, 8, 15)])

    def test_only_capability_adapter_is_model_visible(self) -> None:
        self.assertEqual(HarnessCapabilityTool.runtime_profiles, ("interactive",))
        self.assertEqual(HarnessManualTool.runtime_profiles, ())
        self.assertEqual(HarnessErrorTool.runtime_profiles, ())
        self.assertEqual(HarnessDreamTool.runtime_profiles, ())

    @staticmethod
    async def _record_day(calls: list[date], selected: date) -> None:
        calls.append(selected)

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=root, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    @staticmethod
    def _changeset(suffix: str, *, evidence: bool) -> dict[str, object]:
        digest = hashlib.sha256(suffix.encode()).hexdigest()
        event = {
            "occurred_at": "2026-08-15T08:00:00+00:00",
            "merge_event_id": hashlib.sha256(b"event").hexdigest(),
        } if evidence else {}
        return {
            "date": "2026-08-15", "timezone": "UTC",
            "cutoff_at": "2026-08-16T03:00:00+00:00",
            "source_identity": hashlib.sha256(suffix.encode()).hexdigest()[:16],
            "merge_event_ids": [event["merge_event_id"]] if event else [],
            "invocation_ids": ["a" * 32] if event else [],
            "merged_commits": ["b" * 40] if event else [],
            "changed_files": ["sample.py"] if event else [],
            "changeset_hash": digest,
            "stable_key": f"harness-dream:{suffix}:{digest}",
            "last_event": event,
            "evidence": [{"present": True}] if evidence else [],
        }


if __name__ == "__main__":
    unittest.main()
