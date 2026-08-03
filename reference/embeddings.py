"""OpenAI-compatible Embedding 客户端与持久化后台任务。"""

from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import Sequence
from typing import Protocol

import httpx

from .store import ReferenceStore


class EmbeddingProvider(Protocol):
    model: str

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class OpenAIEmbeddingProvider:
    """只实现 `/embeddings`，不复用或复制对话 Provider 的 ReAct 逻辑。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: int = 60,
        use_system_proxy: bool = False,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not api_key or not model:
            raise ValueError("Embedding base_url、api_key 与 model 均不能为空")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.use_system_proxy = use_system_proxy
        self.proxy_url = proxy_url
        self.transport = transport

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts or any(not value.strip() for value in texts):
            raise ValueError("Embedding 输入不能为空")
        options: dict[str, object] = {
            "timeout": self.timeout_seconds,
            "trust_env": bool(self.use_system_proxy and not self.proxy_url),
        }
        if self.proxy_url:
            options["proxy"] = self.proxy_url
        if self.transport is not None:
            options["transport"] = self.transport
        try:
            async with httpx.AsyncClient(**options) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": list(texts)},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Embedding 服务返回 HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Embedding 请求失败（{type(exc).__name__}）") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError("Embedding 响应 data 数量与输入不一致")
        ordered = sorted(data, key=lambda item: item.get("index", 0) if isinstance(item, dict) else -1)
        vectors: list[tuple[float, ...]] = []
        for item in ordered:
            raw = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(raw, list) or not raw or not all(isinstance(value, (int, float)) for value in raw):
                raise RuntimeError("Embedding 响应包含无效向量")
            vector = tuple(float(value) for value in raw)
            if not all(math.isfinite(value) for value in vector):
                raise RuntimeError("Embedding 响应包含非有限数值")
            vectors.append(vector)
        dimensions = {len(item) for item in vectors}
        if len(dimensions) != 1:
            raise RuntimeError("Embedding 响应向量维度不一致")
        return tuple(vectors)


class ReferenceEmbeddingWorker:
    """Gateway 持有的单 Worker；任务状态全部在 SQLite 中，重启后可续跑。"""

    def __init__(self, store: ReferenceStore, provider: EmbeddingProvider | None) -> None:
        self.store = store
        self.provider = provider
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        if self.provider is None or self._task is not None:
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="reference-embedding-worker")
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

    async def _run(self) -> None:
        assert self.provider is not None
        while not self._closing:
            job = self.store.claim_embedding_job(self.provider.model)
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                document = self.store.search_document(job.document_id)
                vector = (await self.provider.embed((str(document["search_text"]),)))[0]
                self.store.complete_embedding(job, pack_vector(vector), len(vector))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不保存响应正文或凭据，只记录可诊断的异常类型与脱敏消息。
                self.store.fail_embedding(job, f"{type(exc).__name__}: {str(exc)[:800]}")


def pack_vector(vector: Sequence[float]) -> bytes:
    if not vector:
        raise ValueError("向量不能为空")
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(value: bytes, dimensions: int) -> tuple[float, ...]:
    expected = dimensions * 4
    if dimensions <= 0 or len(value) != expected:
        raise ValueError("向量 BLOB 与维度不匹配")
    return tuple(struct.unpack(f"<{dimensions}f", value))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("余弦相似度要求非空且同维向量")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0
