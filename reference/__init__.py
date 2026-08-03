"""全局论文、原文摘录与引用例句资料库。"""

from .models import (
    Author,
    CitationExample,
    CitationExampleCreate,
    EmbeddingJob,
    Paper,
    PaperFile,
    PaperIdentifier,
    PaperUpsert,
    ReferenceSearchHit,
    ReferenceSearchRequest,
    ReferenceSearchResult,
    SourcePassage,
    SourcePassageCreate,
)
from .store import ReferenceStore
from .embeddings import (
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    ReferenceEmbeddingWorker,
    cosine_similarity,
    pack_vector,
    unpack_vector,
)
from .service import ReferenceService
from .factory import build_embedding_provider

__all__ = [
    "Author", "CitationExample", "CitationExampleCreate", "EmbeddingJob", "Paper",
    "PaperFile", "PaperIdentifier", "PaperUpsert", "ReferenceSearchHit",
    "ReferenceSearchRequest", "ReferenceSearchResult", "ReferenceStore", "ReferenceService",
    "SourcePassage", "SourcePassageCreate",
    "EmbeddingProvider", "OpenAIEmbeddingProvider", "ReferenceEmbeddingWorker",
    "cosine_similarity", "pack_vector", "unpack_vector",
    "build_embedding_provider",
]
