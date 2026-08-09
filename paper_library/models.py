"""全局论文库的稳定 Pydantic 数据契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


PaperStatus = Literal[
    "discovered",
    "downloaded",
    "duplicate",
    "unavailable",
    "parse_failed",
    "ocr_required",
    "summarized",
]


class PaperLibraryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PaperCandidate(PaperLibraryModel):
    title: str = Field(min_length=1, max_length=2000)
    pdf_url: str = Field(min_length=1, max_length=4000)
    source_url: str | None = Field(default=None, max_length=4000)
    authors: tuple[str, ...] = ()
    year: int | None = Field(default=None, ge=1000, le=3000)
    venue: str = ""
    abstract: str = ""
    doi: str | None = Field(default=None, max_length=512)
    arxiv_id: str | None = Field(default=None, max_length=128)
    query: str = ""
    source: str = ""

    @field_validator("title", "pdf_url", "source_url", "doi", "arxiv_id")
    @classmethod
    def _strip_values(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class PaperRecord(PaperLibraryModel):
    paper_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str
    stem: str
    directory: str
    pdf_path: str | None = None
    summary_path: str | None = None
    status: PaperStatus = "discovered"
    duplicate_of: str | None = None
    source_url: str | None = None
    pdf_url: str
    final_url: str | None = None
    source: str = ""
    query: str = ""
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str = ""
    abstract: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    discovered_at: datetime
    last_attempt_at: datetime | None = None
    downloaded_at: datetime | None = None
    summarized_at: datetime | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    page_count: int | None = Field(default=None, ge=0)
    pages_read: tuple[int, ...] = ()
    ocr_required: bool = False
    error: str | None = None
    retryable: bool = False
    reference_paper_id: str | None = None
    reference_passage_ids: tuple[str, ...] = ()
    reference_citation_example_ids: tuple[str, ...] = ()


class PaperIndex(PaperLibraryModel):
    version: Literal[1] = 1
    papers: dict[str, PaperRecord] = Field(default_factory=dict)


class PaperLookupResult(PaperLibraryModel):
    paper_id: str
    found: bool
    record: PaperRecord | None = None


class PaperBatchGrant(PaperLibraryModel):
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str | None = None
    paper_ids: tuple[str, ...]
    created_at: datetime


class PaperGrantIndex(PaperLibraryModel):
    version: Literal[1] = 1
    grants: dict[str, PaperBatchGrant] = Field(default_factory=dict)


class PaperDownloadItem(PaperLibraryModel):
    paper_id: str
    status: PaperStatus
    title: str
    path: str | None = None
    duplicate_of: str | None = None
    error: str | None = None


class PaperDownloadResult(PaperLibraryModel):
    batch_id: str
    items: tuple[PaperDownloadItem, ...]


class PaperSummaryUpdate(PaperLibraryModel):
    paper_id: str
    batch_id: str
    status: Literal["summarized", "parse_failed", "ocr_required"]
    markdown: str | None = None
    page_count: int | None = Field(default=None, ge=0)
    pages_read: tuple[int, ...] = ()
    error: str | None = None
    reference_paper_id: str | None = None
    reference_passage_ids: tuple[str, ...] = ()
    reference_citation_example_ids: tuple[str, ...] = ()

    @field_validator("markdown")
    @classmethod
    def _summary_required(cls, value: str | None, info):
        if info.data.get("status") == "summarized" and not (value or "").strip():
            raise ValueError("summarized 状态必须提供 Markdown 总结")
        return value
