"""Explicitly configured semantic memory projection."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import Any

from reference.embeddings import (
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    pack_vector,
)

from .long_term import utc_now
from .structured import StructuredMemoryStore
from backup import QuiesceResult


def build_memory_embedding_provider(config: Any) -> OpenAIEmbeddingProvider | None:
    model = str(getattr(config, "memory_embedding_model", "") or "").strip()
    base_url = str(getattr(config, "memory_embedding_base_url", "") or "").strip()
    api_key = str(getattr(config, "memory_embedding_api_key", "") or "").strip()
    # Deliberately do not fall back to the chat or Reference credentials.
    if not model or not base_url or not api_key:
        return None
    return OpenAIEmbeddingProvider(
        base_url, api_key, model,
        use_system_proxy=bool(getattr(config, "use_system_proxy", False)),
        proxy_url=getattr(config, "proxy_url", None),
    )


class MemoryEmbeddingWorker:
    def __init__(
        self, store: StructuredMemoryStore, provider: EmbeddingProvider | None,
        *, version: int = 1,
    ) -> None:
        self.store = store
        self.provider = provider
        self.version = version
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._job_lock = asyncio.Lock()
        self._maintenance_epoch: int | None = None
        self._materialized_watermark: int | None = None

    async def start(self) -> None:
        if self.provider is None or self._task is not None:
            return
        self.store.ensure_embedding_jobs(self.provider.model, self.version)
        self._materialized_watermark = self.store.watermark()
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="memory-embedding-worker")
        self.wake()

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def drain_once(self) -> bool:
        if self.provider is None:
            return False
        if self._maintenance_epoch is not None:
            return False
        async with self._job_lock:
            return await self._drain_once_locked()

    async def _drain_once_locked(self) -> bool:
        assert self.provider is not None
        watermark = self.store.watermark()
        if self._materialized_watermark != watermark:
            self.store.ensure_embedding_jobs(self.provider.model, self.version)
            self._materialized_watermark = watermark
        with self.store.transaction() as db:
            job = db.execute(
                """SELECT * FROM memory_embedding_jobs
                   WHERE embedding_model=? AND embedding_version=?
                     AND status IN ('pending','failed','running')
                   ORDER BY created_at,job_id LIMIT 1""",
                (self.provider.model, self.version),
            ).fetchone()
            if job is None:
                return False
            db.execute(
                "UPDATE memory_embedding_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE job_id=?",
                (utc_now(), job["job_id"]),
            )
            record = db.execute(
                "SELECT content,content_hash,revision FROM memory_records WHERE memory_id=?",
                (job["memory_id"],),
            ).fetchone()
        try:
            if record is None or int(record["revision"]) != int(job["target_revision"]):
                raise RuntimeError("embedding job no longer matches canonical revision")
            vector = (await self.provider.embed((str(record["content"]),)))[0]
            with closing(sqlite3.connect(self.store.index_path)) as index:
                index.execute(
                    """INSERT INTO memory_embeddings VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(memory_id,embedding_model,embedding_version) DO UPDATE SET
                       dimensions=excluded.dimensions,vector=excluded.vector,
                       content_hash=excluded.content_hash,created_at=excluded.created_at""",
                    (job["memory_id"], self.provider.model, self.version, len(vector),
                     pack_vector(vector), record["content_hash"], utc_now()),
                )
                index.commit()
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE memory_embedding_jobs SET status='ready',last_error=NULL,updated_at=? WHERE job_id=?",
                    (utc_now(), job["job_id"]),
                )
            return True
        except Exception as exc:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE memory_embedding_jobs SET status='failed',last_error=?,updated_at=? WHERE job_id=?",
                    (f"{type(exc).__name__}: {str(exc)[:500]}", utc_now(), job["job_id"]),
                )
            return False

    async def quiesce(self, maintenance_epoch: int) -> QuiesceResult:
        if self._maintenance_epoch is not None and maintenance_epoch <= self._maintenance_epoch:
            return QuiesceResult(
                participant="memory_embedding", maintenance_epoch=maintenance_epoch,
                acknowledged=maintenance_epoch == self._maintenance_epoch,
                stale=maintenance_epoch < self._maintenance_epoch,
            )
        self._maintenance_epoch = maintenance_epoch
        self._wake.set()
        async with self._job_lock:
            pass
        return QuiesceResult(
            participant="memory_embedding", maintenance_epoch=maintenance_epoch,
            acknowledged=True, safe_boundary="embedding_job_persisted",
        )

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None
            self._wake.set()

    async def _run(self) -> None:
        while not self._closing:
            if self._maintenance_epoch is not None:
                self._wake.clear()
                await self._wake.wait()
                continue
            if await self.drain_once():
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
