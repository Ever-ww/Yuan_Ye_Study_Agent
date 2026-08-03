"""论文资料库的稳定 Pydantic 数据契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PaperIdentifier(ReferenceModel):
    scheme: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=512)


class Author(ReferenceModel):
    display_name: str = Field(min_length=1, max_length=300)
    given_name: str = ""
    family_name: str = ""
    orcid: str | None = None
    affiliation: str | None = None


class PaperFile(ReferenceModel):
    workspace_hash: str
    workspace_root: str
    relative_path: str
    absolute_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = "application/pdf"
    size_bytes: int = Field(ge=0)
    is_primary: bool = True
    source_session_id: str | None = None


class PaperUpsert(ReferenceModel):
    title: str = Field(min_length=1, max_length=2000)
    abstract: str = ""
    publication_year: int | None = Field(default=None, ge=1000, le=3000)
    publication_date: str | None = None
    language: str = ""
    venue: str = ""
    publisher: str = ""
    license: str = ""
    canonical_url: str | None = None
    pdf_url: str | None = None
    citation_key: str | None = None
    identifiers: tuple[PaperIdentifier, ...] = ()
    authors: tuple[Author, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_session_id: str | None = None
    source_workspace: str | None = None


class Paper(ReferenceModel):
    paper_id: str
    title: str
    abstract: str = ""
    publication_year: int | None = None
    publication_date: str | None = None
    language: str = ""
    venue: str = ""
    publisher: str = ""
    license: str = ""
    canonical_url: str | None = None
    pdf_url: str | None = None
    citation_key: str | None = None
    status: Literal["active", "archived"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    identifiers: tuple[PaperIdentifier, ...] = ()
    authors: tuple[Author, ...] = ()
    files: tuple[PaperFile, ...] = ()
    tags: tuple[str, ...] = ()
    source_session_id: str | None = None
    source_workspace: str | None = None
    created_at: str
    updated_at: str


class SourcePassageCreate(ReferenceModel):
    paper_id: str
    text: str = Field(min_length=1, max_length=100_000)
    context_before: str = ""
    context_after: str = ""
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section: str = ""
    paragraph: str = ""
    language: str = ""
    translation: str = ""
    extraction_method: str = "manual"
    verification_status: Literal["unverified", "verified", "rejected"] = "unverified"
    source_session_id: str | None = None
    source_workspace: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)


class SourcePassage(SourcePassageCreate):
    passage_id: str
    text_hash: str
    created_at: str
    updated_at: str


class CitationExampleCreate(ReferenceModel):
    paper_id: str
    text: str = Field(min_length=1, max_length=100_000)
    language: str = ""
    citation_style: str = ""
    claim: str = ""
    note: str = ""
    created_by: Literal["user", "assistant", "import"] = "assistant"
    verification_status: Literal["unverified", "verified", "rejected"] = "unverified"
    source_passage_ids: tuple[str, ...] = ()
    source_session_id: str | None = None
    source_workspace: str | None = None


class CitationExample(CitationExampleCreate):
    example_id: str
    created_at: str
    updated_at: str


class ReferenceSearchRequest(ReferenceModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["rrf", "weighted", "separate"] = "rrf"
    entity_types: tuple[Literal["paper", "passage", "citation_example"], ...] = ()
    author: str | None = None
    year_from: int | None = Field(default=None, ge=1000, le=3000)
    year_to: int | None = Field(default=None, ge=1000, le=3000)
    tag: str | None = None
    language: str | None = None
    top_k: int = Field(default=10, ge=1, le=100)


class ReferenceSearchHit(ReferenceModel):
    document_id: str
    entity_type: Literal["paper", "passage", "citation_example"]
    entity_id: str
    paper_id: str
    title: str
    text: str
    lexical_score: float | None = None
    semantic_score: float | None = None
    combined_score: float | None = None


class ReferenceSearchResult(ReferenceModel):
    query: str
    mode: Literal["rrf", "weighted", "separate"]
    semantic_available: bool
    semantic_error: str | None = None
    results: tuple[ReferenceSearchHit, ...] = ()
    lexical_results: tuple[ReferenceSearchHit, ...] = ()
    semantic_results: tuple[ReferenceSearchHit, ...] = ()


class EmbeddingJob(ReferenceModel):
    job_id: str
    document_id: str
    model: str
    content_hash: str
    status: Literal["pending", "running", "failed", "completed"]
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
