"""Workspace-scoped PDF import, summaries, and temporary Agent attachments."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from pypdf import PdfReader

from bootstrap import workspace_id
from paper_library import PaperIndex, sanitize_paper_title
from reference import Paper, PaperFile, PaperUpsert, ReferenceStore


MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENT_TEXT = 60_000
ATTACHMENT_TTL = timedelta(hours=24)


class PaperWebService:
    def __init__(
        self, workspace_root: Path, store: ReferenceStore,
        *, attachments_root: Path | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.store = store
        self.root = self.workspace_root / ".yy" / "papers"
        # Agent attachments are disposable Turn context. Keeping them out of
        # the Workspace prevents backups and Paper Library scans retaining
        # what the user only attached temporarily.
        self.attachments_root = attachments_root or (
            Path(tempfile.gettempdir()) / "yy-agent" / "attachments"
            / workspace_id(self.workspace_root)
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.attachments_root.mkdir(parents=True, exist_ok=True)

    def import_pdf(self, body: bytes, original_name: str) -> dict[str, Any]:
        reader = _pdf_reader(body)
        title = _paper_title(reader, original_name)
        digest = hashlib.sha256(body).hexdigest()
        existing = self.store.paper_by_file_hash(digest)
        if existing is not None:
            if existing.status == "archived":
                self.store.archive(existing.paper_id, archived=False)
                existing = self.store.patch_paper_metadata(existing.paper_id, {
                    "deleted_at": None, "purge_after": None,
                })
            selected = next(
                (item for item in existing.files if item.sha256 == digest), None,
            )
            return {
                "paper": existing,
                "content_hash": digest,
                "filename": Path(selected.relative_path).name if selected else Path(original_name).name,
                "page_count": len(reader.pages),
            "created": False,
        }
        paper = self.store.upsert_paper(PaperUpsert(
            title=title,
            metadata={"imported_via": "read", "original_filename": Path(original_name).name},
            source_workspace=workspace_id(self.workspace_root),
        ), deduplicate=False)
        directory = self.root / paper.paper_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{sanitize_paper_title(title)}.pdf"
        if target.exists() and _sha256(target) != digest:
            target = directory / f"{sanitize_paper_title(title)}-{digest[:8]}.pdf"
        if not target.exists():
            _atomic_bytes(target, body)
        relative = target.relative_to(self.workspace_root).as_posix()
        self.store.add_file(paper.paper_id, PaperFile(
            workspace_hash=workspace_id(self.workspace_root),
            workspace_root=str(self.workspace_root),
            relative_path=relative,
            absolute_path=str(target),
            sha256=digest,
            mime_type="application/pdf",
            size_bytes=len(body),
            is_primary=True,
        ))
        paper = self.store.patch_paper_metadata(paper.paper_id, {
            "pdf_title_source": _title_source(reader, original_name),
        })
        return {
            "paper": paper,
            "content_hash": digest,
            "filename": target.name,
            "page_count": len(reader.pages),
            "created": True,
        }

    def trash_paper(self, paper_id: str, *, retention_days: int = 7) -> Paper:
        """Hide a paper now; the physical files are purged after the retention window."""
        self.store.get_paper(paper_id)
        purge_after = datetime.now(timezone.utc) + timedelta(days=retention_days)
        self.store.archive(paper_id, archived=True)
        return self.store.patch_paper_metadata(paper_id, {
            "deleted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "purge_after": purge_after.isoformat(timespec="seconds"),
        })

    def purge_expired_papers(self) -> int:
        """Remove only papers explicitly marked for expiry, never legacy archives."""
        now = datetime.now(timezone.utc)
        removed = 0
        for paper in self.store.list_papers(include_archived=True, limit=201):
            if paper.status != "archived":
                continue
            raw_deadline = paper.metadata.get("purge_after")
            if not isinstance(raw_deadline, str):
                continue
            try:
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
            except ValueError:
                continue
            if deadline > now:
                continue
            candidate = self.root / paper.paper_id
            if candidate.is_symlink():
                continue
            directory = candidate.resolve()
            root = self.root.resolve()
            if root not in directory.parents or directory.is_symlink():
                continue
            if directory.is_dir():
                shutil.rmtree(directory)
            self.store.delete_paper(paper.paper_id)
            removed += 1
        return removed

    def extracted_text(self, paper_id: str, *, max_chars: int = MAX_ATTACHMENT_TEXT) -> str:
        paper = self.store.get_paper(paper_id)
        selected = next((item for item in paper.files if item.is_primary), None)
        if selected is None:
            raise FileNotFoundError("Paper does not have a primary PDF")
        path = Path(selected.absolute_path).resolve()
        if not path.is_relative_to(self.root.resolve()) or path.is_symlink() or not path.is_file():
            raise PermissionError("Paper PDF is outside the Workspace library")
        return _extract_text(PdfReader(path), max_chars=max_chars)

    def summary(self, paper_id: str) -> dict[str, Any]:
        paper = self.store.get_paper(paper_id)
        path = self.root / paper_id / "summary.md"
        if not path.is_file():
            path = self._legacy_summary_path(paper_id) or path
        metadata = paper.metadata
        return {
            "paper_id": paper_id,
            "status": metadata.get("summary_status", "completed" if path.is_file() else "missing"),
            "content": path.read_text(encoding="utf-8") if path.is_file() else "",
            "run_id": metadata.get("summary_run_id"),
            "error": metadata.get("summary_error"),
            "updated_at": metadata.get("summary_updated_at"),
        }

    def _legacy_summary_path(self, paper_id: str) -> Path | None:
        index_path = self.root / "index.json"
        try:
            index = PaperIndex.model_validate_json(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        record = next(
            (item for item in index.papers.values() if item.reference_paper_id == paper_id),
            None,
        )
        if record is None or not record.summary_path:
            return None
        path = (self.root / Path(record.summary_path)).resolve()
        root = self.root.resolve()
        if root not in path.parents or path.is_symlink() or not path.is_file():
            return None
        return path

    def mark_summary(self, paper_id: str, *, status: str, run_id: str | None = None,
                     error: str | None = None) -> None:
        self.store.patch_paper_metadata(paper_id, {
            "summary_status": status,
            "summary_run_id": run_id,
            "summary_error": error,
            "summary_updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    def write_summary(self, paper_id: str, content: str, *, run_id: str) -> dict[str, Any]:
        directory = self.root / paper_id
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_text(directory / "summary.md", content.rstrip() + "\n")
        self.mark_summary(paper_id, status="completed", run_id=run_id, error=None)
        return self.summary(paper_id)

    def upload_attachment(self, body: bytes, original_name: str) -> dict[str, Any]:
        reader = _pdf_reader(body, limit=MAX_PDF_BYTES)
        self._prune_attachments()
        attachment_id = uuid4().hex
        digest = hashlib.sha256(body).hexdigest()
        path = self.attachments_root / f"{attachment_id}.pdf"
        _atomic_bytes(path, body)
        metadata = {
            "attachment_id": attachment_id,
            "filename": Path(original_name).name or "attachment.pdf",
            "content_hash": digest,
            "text": _extract_text(reader, max_chars=MAX_ATTACHMENT_TEXT),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _atomic_text(path.with_suffix(".json"), json.dumps(metadata, ensure_ascii=False))
        return {key: value for key, value in metadata.items() if key != "text"}

    def resolve_attachments(self, attachment_ids: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
        values: list[dict[str, Any]] = []
        for attachment_id in dict.fromkeys(attachment_ids):
            if not re.fullmatch(r"[0-9a-f]{32}", attachment_id):
                raise ValueError("Invalid temporary attachment id")
            metadata_path = self.attachments_root / f"{attachment_id}.json"
            pdf_path = self.attachments_root / f"{attachment_id}.pdf"
            if not metadata_path.is_file() or not pdf_path.is_file():
                raise FileNotFoundError("Temporary attachment expired or is unavailable")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("content_hash") != _sha256(pdf_path):
                raise ValueError("Temporary attachment content changed")
            values.append(metadata)
        return tuple(values)

    def _prune_attachments(self) -> None:
        cutoff = datetime.now(timezone.utc) - ATTACHMENT_TTL
        for metadata_path in self.attachments_root.glob("*.json"):
            try:
                value = json.loads(metadata_path.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
                if created >= cutoff:
                    continue
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
            metadata_path.unlink(missing_ok=True)
            metadata_path.with_suffix(".pdf").unlink(missing_ok=True)


def _pdf_reader(body: bytes, *, limit: int = MAX_PDF_BYTES) -> PdfReader:
    if not body or len(body) > limit or not body.lstrip().startswith(b"%PDF-"):
        raise ValueError("Only non-empty PDF files up to 100 MB are supported")
    try:
        reader = PdfReader(BytesIO(body))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF files are not supported")
        if not reader.pages:
            raise ValueError("PDF has no pages")
        return reader
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The uploaded file is not a readable PDF") from exc


def _paper_title(reader: PdfReader, original_name: str) -> str:
    metadata_title = str(getattr(reader.metadata, "title", "") or "").strip()
    if _plausible_title(metadata_title):
        return metadata_title
    first_page = (reader.pages[0].extract_text() or "").replace("\x00", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in first_page.splitlines()]
    candidates = [line for line in lines[:20] if _plausible_title(line)]
    if candidates:
        return max(candidates[:6], key=len)
    fallback = Path(original_name).stem.strip()
    return fallback or "Untitled Paper"


def _title_source(reader: PdfReader, original_name: str) -> str:
    title = str(getattr(reader.metadata, "title", "") or "").strip()
    if _plausible_title(title):
        return "pdf_metadata"
    first_page = (reader.pages[0].extract_text() or "").strip()
    return "first_page" if first_page else "filename"


def _plausible_title(value: str) -> bool:
    clean = re.sub(r"\s+", " ", value).strip()
    return 8 <= len(clean) <= 300 and not re.fullmatch(r"(?:arxiv:)?[\d.]+", clean, re.I)


def _extract_text(reader: PdfReader, *, max_chars: int) -> str:
    parts: list[str] = []
    length = 0
    for number, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        block = f"[Page {number}]\n{text}\n"
        remaining = max_chars - length
        if remaining <= 0:
            break
        parts.append(block[:remaining])
        length += min(len(block), remaining)
    return "\n".join(parts)


def _atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
