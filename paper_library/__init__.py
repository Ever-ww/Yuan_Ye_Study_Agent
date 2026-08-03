"""全局论文 PDF、总结与索引的公共接口。"""

from .models import (
    PaperBatchGrant,
    PaperCandidate,
    PaperDownloadItem,
    PaperDownloadResult,
    PaperIndex,
    PaperLookupResult,
    PaperRecord,
    PaperSummaryUpdate,
)
from .service import (
    PaperLibraryService,
    normalize_arxiv,
    normalize_doi,
    sanitize_paper_title,
    stable_paper_id,
)

__all__ = [
    "PaperBatchGrant",
    "PaperCandidate",
    "PaperDownloadItem",
    "PaperDownloadResult",
    "PaperIndex",
    "PaperLibraryService",
    "PaperLookupResult",
    "PaperRecord",
    "PaperSummaryUpdate",
    "normalize_arxiv",
    "normalize_doi",
    "sanitize_paper_title",
    "stable_paper_id",
]
