from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cron import CronDispatch, CronJobCreateRequest, CronJobEditRequest, CronSchedule, CronService, CronStore
from cron.models import utc_iso


class DurableCronDispatchTests(unittest.IsolatedAsyncioTestCase):
  async def test_profile_edit_preserves_schedule_cursor_and_delete_cancels_unbound_dispatch(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        root = Path(value)
        store = CronStore(root)
        await store.ensure()
        service = CronService(store)
        job = await service.create(CronJobCreateRequest(
            project_id="project", name="durable", prompt="work",
            schedule=CronSchedule(kind="interval", interval_seconds=3600),
        ))
        cursor = job.next_run_at
        edited = await service.edit(job.job_id, CronJobEditRequest(prompt="changed"))
        self.assertEqual(edited.next_run_at, cursor)

        dispatch = CronDispatch(
            dispatch_id="dispatch", job_id=edited.job_id, job_revision=edited.revision,
            trigger="run_now", job_snapshot=edited.model_dump(mode="json"),
            request_hash="request", created_at=utc_iso(),
        )
        await store.due_materialize(edited, [dispatch], edited.next_run_at, now=utc_iso())
        await service.remove(edited.job_id)
        self.assertEqual((await store.dispatch("dispatch")).status, "cancelled")


  async def test_claim_is_unbound_until_binding_receipt_is_written(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        root = Path(value)
        store = CronStore(root)
        await store.ensure()
        service = CronService(store)
        job = await service.create(CronJobCreateRequest(
            project_id="project", name="claim", prompt="work",
            schedule=CronSchedule(kind="interval", interval_seconds=3600),
        ))
        dispatch = CronDispatch(
            dispatch_id="claim-dispatch", job_id=job.job_id, job_revision=job.revision,
            trigger="run_now", job_snapshot=job.model_dump(mode="json"),
            request_hash="request", created_at=utc_iso(),
        )
        await store.due_materialize(job, [dispatch], job.next_run_at, now=utc_iso())
        claimed = await store.claim_next(job.job_id, epoch="gateway")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.status, "claimed")
        self.assertIsNone(claimed.run_id)
