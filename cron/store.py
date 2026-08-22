"""SQLite repository for durable Cron jobs and dispatches."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import CronDispatch, CronJob, HeartbeatState, utc_iso, utc_now


class CronStore:
    """Cron repository sharing the Gateway SQLite database.

    The legacy jobs.json is intentionally only read during the one-time import.
    """

    def __init__(
        self,
        agent_root: Path,
        *,
        database_path: Path | None = None,
        heartbeat_seconds: int = 60,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.directory = self.agent_root / ".yy" / "cron"
        self.path = self.directory / "jobs.json"
        self.database_path = (database_path or self.agent_root / ".yy" / "gateway" / "gateway.sqlite3").resolve()
        self.heartbeat_seconds = heartbeat_seconds

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    async def ensure(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cron_heartbeat (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    status TEXT NOT NULL CHECK(status IN ('stopped','running','unhealthy')),
                    interval_seconds INTEGER NOT NULL,
                    last_tick_at TEXT,
                    next_tick_at TEXT,
                    last_error TEXT
                );
                INSERT OR IGNORE INTO cron_heartbeat(singleton,status,interval_seconds)
                VALUES(1,'stopped',60);
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('scheduled','paused','completed','deleted')),
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    next_run_at TEXT,
                    current_dispatch_id TEXT,
                    current_run_id TEXT,
                    job_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS cron_jobs_project_idx ON cron_jobs(project_id,state);
                CREATE TABLE IF NOT EXISTS cron_dispatches (
                    dispatch_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    job_revision INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    scheduled_for TEXT,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','claimed','running','succeeded','failed','cancelled','skipped','recovery_required')),
                    claim_token TEXT,
                    claim_epoch TEXT,
                    claim_expires_at TEXT,
                    session_id TEXT,
                    run_id TEXT UNIQUE,
                    operation_id TEXT UNIQUE,
                    attempt_id TEXT UNIQUE,
                    retry_of_dispatch_id TEXT,
                    dispatch_json TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY(job_id) REFERENCES cron_jobs(job_id),
                    FOREIGN KEY(retry_of_dispatch_id) REFERENCES cron_dispatches(dispatch_id)
                );
                CREATE INDEX IF NOT EXISTS cron_dispatches_job_idx
                    ON cron_dispatches(job_id,status,created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS cron_dispatch_unique_period
                    ON cron_dispatches(job_id,scheduled_for) WHERE scheduled_for IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS cron_one_active_dispatch_per_job
                    ON cron_dispatches(job_id)
                    WHERE status IN ('claimed','running','recovery_required');
                CREATE TABLE IF NOT EXISTS cron_migration_records (
                    migration_name TEXT PRIMARY KEY,
                    source_hash TEXT,
                    result_json TEXT NOT NULL,
                    migrated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "UPDATE cron_heartbeat SET interval_seconds=? WHERE singleton=1",
                (self.heartbeat_seconds,),
            )
        await self._migrate_legacy_json_once()

    async def _migrate_legacy_json_once(self) -> None:
        with self._connect() as connection:
            done = connection.execute(
                "SELECT 1 FROM cron_migration_records WHERE migration_name='jobs_json_v1'",
            ).fetchone()
            if done is not None:
                return
        if not self.path.exists():
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO cron_migration_records VALUES(?,?,?,?)",
                    ("jobs_json_v1", None, json.dumps({"status": "absent"}), utc_iso()),
                )
            return
        raw = self.path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw.decode("utf-8"))
            jobs = payload.get("jobs", {}) if isinstance(payload, dict) else {}
            if not isinstance(jobs, dict):
                raise ValueError("jobs must be an object")
        except Exception as exc:
            # Do not rewrite corrupt legacy state. Record the condition so status is visible.
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO cron_migration_records VALUES(?,?,?,?)",
                    ("jobs_json_v1", digest, json.dumps({"status": "invalid", "error": str(exc)}), utc_iso()),
                )
                connection.execute("UPDATE cron_heartbeat SET status='unhealthy',last_error=? WHERE singleton=1", (f"legacy jobs.json invalid: {exc}",))
            return
        from .models import CronJob, CronRuntimeProfile, CronSchedule
        now = utc_iso()
        imported = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for job_id, old in jobs.items():
                if not isinstance(old, dict):
                    continue
                try:
                    schedule = CronSchedule.model_validate(old.get("schedule"), strict=True)
                    grants = tuple(old.get("preapproved_tools", ()))
                    profile = CronRuntimeProfile(allowed_tools=grants, preapproved_tools=grants)
                    state = old.get("state", "scheduled")
                    if state not in {"scheduled", "paused", "completed"}:
                        state = "scheduled"
                    job = CronJob(
                        job_id=str(old.get("job_id", job_id)), project_id=str(old["project_id"]),
                        name=str(old["name"]), prompt=str(old["prompt"]), schedule=schedule,
                        state=state, runtime_profile=profile,
                        created_at=str(old.get("created_at", now)), updated_at=now,
                        next_run_at=old.get("next_run_at"), last_run_id=old.get("last_run_id"),
                        last_session_id=old.get("last_session_id"), last_status=old.get("last_status"),
                        last_error=old.get("last_error"), run_count=int(old.get("run_count", 0)),
                        failure_count=int(old.get("failure_count", 0)),
                        overlap_skipped=int(old.get("skipped_overlap_count", 0)),
                    )
                except Exception:
                    continue
                self._insert_job(connection, job)
                imported += 1
            heartbeat = payload.get("heartbeat", {}) if isinstance(payload, dict) else {}
            if isinstance(heartbeat, dict):
                connection.execute(
                    "UPDATE cron_heartbeat SET status=?,last_tick_at=?,next_tick_at=?,last_error=? WHERE singleton=1",
                    (heartbeat.get("status", "stopped"), heartbeat.get("last_tick_at"), heartbeat.get("next_tick_at"), heartbeat.get("last_error")),
                )
            connection.execute(
                "INSERT INTO cron_migration_records VALUES(?,?,?,?)",
                ("jobs_json_v1", digest, json.dumps({"status": "imported", "jobs": imported}), utc_iso()),
            )
            connection.commit()

    def _insert_job(self, connection: sqlite3.Connection, job: CronJob) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO cron_jobs(job_id,project_id,state,revision,next_run_at,current_dispatch_id,current_run_id,job_json,created_at,updated_at,deleted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job.job_id, job.project_id, job.state, job.revision, job.next_run_at,
             job.current_dispatch_id, job.current_run_id, job.model_dump_json(),
             job.created_at, job.updated_at, job.deleted_at),
        )

    def _insert_dispatch(self, connection: sqlite3.Connection, dispatch: CronDispatch) -> None:
        connection.execute(
            "INSERT INTO cron_dispatches(dispatch_id,job_id,job_revision,trigger,scheduled_for,status,claim_token,claim_epoch,claim_expires_at,session_id,run_id,operation_id,attempt_id,retry_of_dispatch_id,dispatch_json,revision,created_at,claimed_at,started_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dispatch.dispatch_id, dispatch.job_id, dispatch.job_revision, dispatch.trigger,
             dispatch.scheduled_for, dispatch.status, dispatch.claim_token, dispatch.claim_epoch,
             dispatch.claim_expires_at, dispatch.session_id, dispatch.run_id, dispatch.operation_id,
             dispatch.attempt_id, dispatch.retry_of_dispatch_id, dispatch.model_dump_json(),
             dispatch.revision, dispatch.created_at, dispatch.claimed_at, dispatch.started_at, dispatch.completed_at),
        )

    def _replace_dispatch(self, connection: sqlite3.Connection, dispatch: CronDispatch, expected_revision: int) -> None:
        cursor = connection.execute(
            "UPDATE cron_dispatches SET status=?,claim_token=?,claim_epoch=?,claim_expires_at=?,session_id=?,run_id=?,operation_id=?,attempt_id=?,dispatch_json=?,revision=?,claimed_at=?,started_at=?,completed_at=? WHERE dispatch_id=? AND revision=?",
            (dispatch.status, dispatch.claim_token, dispatch.claim_epoch, dispatch.claim_expires_at,
             dispatch.session_id, dispatch.run_id, dispatch.operation_id, dispatch.attempt_id,
             dispatch.model_dump_json(), dispatch.revision, dispatch.claimed_at, dispatch.started_at,
             dispatch.completed_at, dispatch.dispatch_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Cron dispatch revision conflict")

    async def heartbeat(self) -> HeartbeatState:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM cron_heartbeat WHERE singleton=1").fetchone()
        payload = dict(row)
        payload.pop("singleton", None)
        return HeartbeatState(**payload)

    async def set_heartbeat(self, heartbeat: HeartbeatState) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE cron_heartbeat SET status=?,interval_seconds=?,last_tick_at=?,next_tick_at=?,last_error=? WHERE singleton=1",
                               (heartbeat.status, heartbeat.interval_seconds, heartbeat.last_tick_at, heartbeat.next_tick_at, heartbeat.last_error))

    async def jobs(self, project_id: str | None = None, *, include_deleted: bool = False) -> tuple[CronJob, ...]:
        query = "SELECT job_json FROM cron_jobs"
        args: list[Any] = []
        where: list[str] = []
        if project_id:
            where.append("project_id=?"); args.append(project_id)
        if not include_deleted:
            where.append("state!='deleted'")
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY json_extract(job_json,'$.name'),job_id"
        with self._connect() as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        return tuple(CronJob.model_validate_json(str(row["job_json"]), strict=True) for row in rows)

    async def job(self, job_id: str) -> CronJob:
        with self._connect() as connection:
            row = connection.execute("SELECT job_json FROM cron_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown Cron job: {job_id}")
        return CronJob.model_validate_json(str(row["job_json"]), strict=True)

    async def save_job(self, job: CronJob, *, expected_revision: int | None = None) -> CronJob:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_revision is None:
                self._insert_job(connection, job)
            else:
                cursor = connection.execute(
                    "UPDATE cron_jobs SET state=?,revision=?,next_run_at=?,current_dispatch_id=?,current_run_id=?,job_json=?,updated_at=?,deleted_at=? WHERE job_id=? AND revision=?",
                    (job.state, job.revision, job.next_run_at, job.current_dispatch_id, job.current_run_id,
                     job.model_dump_json(), job.updated_at, job.deleted_at, job.job_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    connection.rollback(); raise RuntimeError("Cron job revision conflict")
            connection.commit()
        return job

    async def dispatches(self, job_id: str, *, limit: int = 100) -> tuple[CronDispatch, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT dispatch_json FROM cron_dispatches WHERE job_id=? ORDER BY created_at DESC LIMIT ?", (job_id, limit),
            ).fetchall()
        return tuple(CronDispatch.model_validate_json(str(row["dispatch_json"]), strict=True) for row in rows)

    async def active_dispatches(self) -> tuple[CronDispatch, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT dispatch_json FROM cron_dispatches WHERE status IN ('claimed','running','recovery_required') ORDER BY created_at",
            ).fetchall()
        return tuple(CronDispatch.model_validate_json(str(row["dispatch_json"]), strict=True) for row in rows)

    async def due_materialize(self, job: CronJob, dispatches: list[CronDispatch], next_run_at: str | None, *, now: str) -> None:
        """Atomically append scheduled dispatches and move the job's materialization cursor."""
        missed = sum(
            dispatch.coalesced_count
            for dispatch in dispatches
            if dispatch.status == "skipped"
        )
        overlap_skipped = sum(
            dispatch.coalesced_count
            for dispatch in dispatches
            if dispatch.status == "skipped" and dispatch.error == "overlap_skipped"
        )
        updated = job.model_copy(update={
            "next_run_at": next_run_at,
            "missed_count": job.missed_count + missed,
            "overlap_skipped": job.overlap_skipped + overlap_skipped,
            "updated_at": now,
            "revision": job.revision + 1,
        })
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM cron_jobs WHERE job_id=?", (job.job_id,)).fetchone()
            if row is None or int(row["revision"]) != job.revision:
                connection.rollback(); raise RuntimeError("Cron job revision conflict")
            for dispatch in dispatches:
                self._insert_dispatch(connection, dispatch)
            cursor = connection.execute(
                "UPDATE cron_jobs SET revision=?,next_run_at=?,job_json=?,updated_at=? WHERE job_id=? AND revision=?",
                (updated.revision, updated.next_run_at, updated.model_dump_json(), now, job.job_id, job.revision),
            )
            if cursor.rowcount != 1:
                connection.rollback(); raise RuntimeError("Cron job revision conflict")
            connection.commit()

    async def claim_next(self, job_id: str, *, epoch: str, lease_seconds: int = 120) -> CronDispatch | None:
        now = utc_now(); now_s = utc_iso(now); expiry = utc_iso(now + timedelta(seconds=lease_seconds))
        from uuid import uuid4
        token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job_row = connection.execute("SELECT job_json FROM cron_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job_row is None:
                connection.rollback(); return None
            job = CronJob.model_validate_json(str(job_row["job_json"]), strict=True)
            if job.state in {"paused", "completed", "deleted"} or job.current_dispatch_id:
                connection.rollback(); return None
            row = connection.execute("SELECT dispatch_json FROM cron_dispatches WHERE job_id=? AND status='pending' ORDER BY created_at LIMIT 1", (job_id,)).fetchone()
            if row is None:
                connection.rollback(); return None
            old = CronDispatch.model_validate_json(str(row["dispatch_json"]), strict=True)
            claimed = old.model_copy(update={"status": "claimed", "claim_token": token, "claim_epoch": epoch,
                                              "claim_expires_at": expiry, "claimed_at": now_s, "revision": old.revision + 1})
            self._replace_dispatch(connection, claimed, old.revision)
            connection.commit()
        return claimed

    async def dispatch(self, dispatch_id: str) -> CronDispatch:
        with self._connect() as connection:
            row = connection.execute("SELECT dispatch_json FROM cron_dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown Cron dispatch: {dispatch_id}")
        return CronDispatch.model_validate_json(str(row["dispatch_json"]), strict=True)

    async def dispatch_by_run_id(self, run_id: str) -> CronDispatch:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT dispatch_json FROM cron_dispatches WHERE run_id=?", (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown Cron dispatch for run: {run_id}")
        return CronDispatch.model_validate_json(str(row["dispatch_json"]), strict=True)

    async def mark_terminal(self, dispatch_id: str, *, status: str, result: str | None, error: str | None, completed_at: str | None = None) -> CronDispatch:
        selected = await self.dispatch(dispatch_id)
        if selected.status not in {"running", "recovery_required"}:
            return selected
        completed = completed_at or utc_iso()
        final = selected.model_copy(update={"status": status, "result": result, "error": error, "completed_at": completed, "revision": selected.revision + 1})
        job = await self.job(selected.job_id)
        success = status == "succeeded"
        state = "completed" if job.schedule.kind == "once" and success else job.state
        updated = job.model_copy(update={
            "state": state, "current_dispatch_id": None, "current_run_id": None,
            "last_run_at": completed, "last_run_id": selected.run_id, "last_session_id": selected.session_id,
            "last_status": status, "last_error": error, "run_count": job.run_count + 1,
            "failure_count": job.failure_count + (0 if success else 1),
            "consecutive_failures": 0 if success else job.consecutive_failures + 1,
            "last_success_at": completed if success else job.last_success_at,
            "last_success_run_id": selected.run_id if success else job.last_success_run_id,
            "last_failure_at": None if success else completed,
            "last_failure_run_id": None if success else selected.run_id,
            "updated_at": completed, "revision": job.revision + 1,
        })
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_dispatch(connection, final, selected.revision)
            cursor = connection.execute("UPDATE cron_jobs SET state=?,revision=?,current_dispatch_id=?,current_run_id=?,job_json=?,updated_at=? WHERE job_id=? AND revision=?",
                                        (updated.state, updated.revision, None, None, updated.model_dump_json(), updated.updated_at, updated.job_id, job.revision))
            if cursor.rowcount != 1:
                connection.rollback(); raise RuntimeError("Cron job revision conflict")
            connection.commit()
        return final

    async def bind_running(
        self,
        dispatch_id: str,
        *,
        claim_token: str,
        session_id: str,
        run_id: str,
        operation_id: str,
        attempt_id: str,
    ) -> CronDispatch:
        """Persist the dispatch→Run binding after the StateController transaction commits."""
        selected = await self.dispatch(dispatch_id)
        if selected.status != "claimed" or selected.claim_token != claim_token:
            raise RuntimeError("Cron dispatch claim no longer owned")
        job = await self.job(selected.job_id)
        if job.state == "deleted":
            raise RuntimeError("Cron job deleted before dispatch bind")
        now = utc_iso()
        bound = selected.model_copy(update={
            "status": "running", "session_id": session_id, "run_id": run_id,
            "operation_id": operation_id, "attempt_id": attempt_id,
            "started_at": now, "revision": selected.revision + 1,
        })
        updated = job.model_copy(update={
            "current_dispatch_id": selected.dispatch_id, "current_run_id": run_id,
            "last_run_at": now, "revision": job.revision + 1, "updated_at": now,
        })
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_dispatch(connection, bound, selected.revision)
            cursor = connection.execute(
                "UPDATE cron_jobs SET revision=?,current_dispatch_id=?,current_run_id=?,job_json=?,updated_at=? WHERE job_id=? AND revision=?",
                (updated.revision, updated.current_dispatch_id, updated.current_run_id,
                 updated.model_dump_json(), updated.updated_at, updated.job_id, job.revision),
            )
            if cursor.rowcount != 1:
                connection.rollback(); raise RuntimeError("Cron job revision conflict")
            connection.commit()
        return bound

    async def cancel_unbound(self, dispatch_id: str, *, reason: str) -> CronDispatch:
        selected = await self.dispatch(dispatch_id)
        if selected.status not in {"pending", "claimed"}:
            return selected
        cancelled = selected.model_copy(update={"status": "cancelled", "error": reason,
                                                 "completed_at": utc_iso(), "revision": selected.revision + 1})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_dispatch(connection, cancelled, selected.revision)
            connection.commit()
        return cancelled

    async def mark_recovery_required(self, dispatch_id: str, reason: str) -> CronDispatch:
        selected = await self.dispatch(dispatch_id)
        if selected.status not in {"claimed", "running", "recovery_required"}:
            return selected
        # A claimed dispatch with no binding has no external Agent side effect;
        # it remains claim evidence rather than being fabricated as bound.
        if selected.status == "claimed":
            return selected
        required = selected.model_copy(update={
            "status": "recovery_required", "error": reason,
            "revision": selected.revision + 1,
        })
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_dispatch(connection, required, selected.revision)
            connection.commit()
        return required
