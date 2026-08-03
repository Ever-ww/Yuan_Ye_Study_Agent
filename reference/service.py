"""Reference 领域服务：写入后排队、全文/向量检索与结果融合。"""

from __future__ import annotations

from typing import Any

from .embeddings import EmbeddingProvider, cosine_similarity, unpack_vector
from .models import (
    CitationExample,
    CitationExampleCreate,
    Paper,
    PaperFile,
    PaperUpsert,
    ReferenceSearchHit,
    ReferenceSearchRequest,
    ReferenceSearchResult,
    SourcePassage,
    SourcePassageCreate,
)
from .store import ReferenceStore


class ReferenceService:
    def __init__(
        self,
        store: ReferenceStore,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        keyword_weight: float = 0.4,
        semantic_weight: float = 0.6,
        worker=None,
    ) -> None:
        if keyword_weight < 0 or semantic_weight < 0 or keyword_weight + semantic_weight <= 0:
            raise ValueError("Reference 检索权重必须为非负且总和大于 0")
        self.store = store
        self.embedding_provider = embedding_provider
        self.keyword_weight = keyword_weight
        self.semantic_weight = semantic_weight
        self.worker = worker

    def upsert_paper(self, value: PaperUpsert) -> Paper:
        result = self.store.upsert_paper(value)
        self._queue(self.store.document_ids_for_paper(result.paper_id))
        return result

    def add_file(self, paper_id: str, value: PaperFile) -> PaperFile:
        return self.store.add_file(paper_id, value)

    def add_passage(self, value: SourcePassageCreate) -> SourcePassage:
        result = self.store.add_passage(value)
        self._queue((f"passage:{result.passage_id}",))
        return result

    def add_citation_example(self, value: CitationExampleCreate) -> CitationExample:
        result = self.store.add_citation_example(value)
        self._queue((f"citation_example:{result.example_id}",))
        return result

    def get(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        return self.store.get_bundle(entity_type, entity_id)

    def archive(self, paper_id: str) -> Paper:
        return self.store.archive(paper_id, True)

    def restore(self, paper_id: str) -> Paper:
        return self.store.archive(paper_id, False)

    def reembed(self, paper_id: str | None = None) -> int:
        if self.embedding_provider is None:
            raise RuntimeError("尚未配置 reference_embedding_model 或 Embedding 凭据")
        if paper_id:
            ids = self.store.document_ids_for_paper(paper_id)
            for document_id in ids:
                self.store.queue_embedding(document_id, self.embedding_provider.model, force=True)
            count = len(ids)
        else:
            count = self.store.queue_all_embeddings(self.embedding_provider.model)
        if self.worker is not None:
            self.worker.wake()
        return count

    async def search(self, request: ReferenceSearchRequest) -> ReferenceSearchResult:
        lexical = self.store.lexical_search(request)
        semantic: list[ReferenceSearchHit] = []
        semantic_error: str | None = None
        if self.embedding_provider is not None:
            try:
                query_vector = (await self.embedding_provider.embed((request.query,)))[0]
                for row in self.store.documents_for_embeddings(request):
                    if row["model"] != self.embedding_provider.model:
                        continue
                    vector = unpack_vector(row["vector"], int(row["dimensions"]))
                    score = cosine_similarity(query_vector, vector)
                    semantic.append(ReferenceSearchHit(
                        document_id=row["document_id"], entity_type=row["entity_type"], entity_id=row["entity_id"],
                        paper_id=row["paper_id"], title=row["title"], text=row["body"] or row["abstract"] or row["title"],
                        semantic_score=score,
                    ))
                semantic.sort(key=lambda item: item.semantic_score or -1.0, reverse=True)
            except Exception as exc:
                semantic_error = f"{type(exc).__name__}: {str(exc)[:500]}"
        else:
            semantic_error = "Embedding 未配置"
        semantic_available = bool(self.embedding_provider is not None and semantic_error is None)
        if request.mode == "separate":
            return ReferenceSearchResult(
                query=request.query, mode=request.mode, semantic_available=semantic_available,
                semantic_error=semantic_error, lexical_results=tuple(lexical[:request.top_k]),
                semantic_results=tuple(semantic[:request.top_k]),
            )
        if not semantic_available or not semantic:
            return ReferenceSearchResult(
                query=request.query, mode=request.mode, semantic_available=False,
                semantic_error=semantic_error, results=tuple(lexical[:request.top_k]),
            )
        combined = (
            _rrf(lexical, semantic)
            if request.mode == "rrf"
            else _weighted(lexical, semantic, self.keyword_weight, self.semantic_weight)
        )
        return ReferenceSearchResult(
            query=request.query, mode=request.mode, semantic_available=True,
            results=tuple(combined[:request.top_k]),
        )

    def _queue(self, document_ids: tuple[str, ...]) -> None:
        if self.embedding_provider is None:
            return
        for document_id in document_ids:
            self.store.queue_embedding(document_id, self.embedding_provider.model)
        if self.worker is not None:
            self.worker.wake()


def _rrf(lexical: list[ReferenceSearchHit], semantic: list[ReferenceSearchHit], k: int = 60) -> list[ReferenceSearchHit]:
    scores: dict[str, float] = {}
    hits: dict[str, ReferenceSearchHit] = {}
    for ranked in (lexical, semantic):
        for rank, hit in enumerate(ranked, 1):
            scores[hit.document_id] = scores.get(hit.document_id, 0.0) + 1.0 / (k + rank)
            previous = hits.get(hit.document_id)
            hits[hit.document_id] = _merge(previous, hit)
    return [hits[key].model_copy(update={"combined_score": score}) for key, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)]


def _weighted(
    lexical: list[ReferenceSearchHit], semantic: list[ReferenceSearchHit], keyword_weight: float, semantic_weight: float,
) -> list[ReferenceSearchHit]:
    lexical_values = {hit.document_id: hit.lexical_score or 0.0 for hit in lexical}
    semantic_values = {hit.document_id: hit.semantic_score or 0.0 for hit in semantic}
    lexical_norm = _normalize_scores(lexical_values)
    semantic_norm = _normalize_scores(semantic_values)
    hits: dict[str, ReferenceSearchHit] = {}
    for hit in (*lexical, *semantic):
        hits[hit.document_id] = _merge(hits.get(hit.document_id), hit)
    total = keyword_weight + semantic_weight
    scores = {
        key: (keyword_weight * lexical_norm.get(key, 0.0) + semantic_weight * semantic_norm.get(key, 0.0)) / total
        for key in hits
    }
    return [hits[key].model_copy(update={"combined_score": score}) for key, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)]


def _normalize_scores(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if high == low:
        return {key: 1.0 for key in values}
    return {key: (value - low) / (high - low) for key, value in values.items()}


def _merge(previous: ReferenceSearchHit | None, current: ReferenceSearchHit) -> ReferenceSearchHit:
    if previous is None:
        return current
    return previous.model_copy(update={
        "lexical_score": previous.lexical_score if previous.lexical_score is not None else current.lexical_score,
        "semantic_score": previous.semantic_score if previous.semantic_score is not None else current.semantic_score,
    })
