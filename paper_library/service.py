"""Agent Home 全局论文库：索引、受控下载、解析与原子总结写入。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import ValidationError

from sandbox import WorkspaceLockManager
from .models import (
    PaperBatchGrant,
    PaperCandidate,
    PaperDownloadItem,
    PaperDownloadResult,
    PaperGrantIndex,
    PaperIndex,
    PaperLookupResult,
    PaperRecord,
    PaperSummaryUpdate,
)


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class PaperLibraryService:
    """只允许访问 `<Agent Home>/.yy/papers` 的论文资料服务。"""

    def __init__(
        self,
        agent_root: Path,
        reference_store: Any | None = None,
        locks: WorkspaceLockManager | None = None,
        *,
        downloader: Any,
        reader: Any | None = None,
    ) -> None:
        self.agent_root = agent_root.resolve()
        self.root = self.agent_root / ".yy" / "papers"
        self.index_path = self.root / "index.json"
        self.grants_path = self.root / "grants.json"
        self.reference_store = reference_store
        self.downloader = downloader
        self.locks = locks or WorkspaceLockManager(self.agent_root, state_root=self.agent_root)
        if reader is None:
            from tools.read_file import DocumentReader

            reader = DocumentReader()
        self.reader = reader
        self._grants: dict[str, PaperBatchGrant] = {}
        self.initialize()

    def initialize(self) -> None:
        if self.root.is_symlink():
            raise PermissionError("全局论文库根目录不能是符号链接")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.index_path.is_symlink():
            raise PermissionError("论文库索引不能是符号链接")
        if not self.index_path.exists():
            self._write_index(PaperIndex())
        if self.grants_path.is_symlink():
            raise PermissionError("论文库批次授权索引不能是符号链接")
        if not self.grants_path.exists():
            self._write_grants(PaperGrantIndex(grants=self._recover_legacy_grants()))
        self._grants = dict(self._read_grants().grants)

    async def lookup(self, candidates: tuple[PaperCandidate, ...]) -> tuple[PaperLookupResult, ...]:
        async with self.locks.read(self.index_path):
            index = self._read_index()
        return tuple(
            PaperLookupResult(
                paper_id=(paper_id := stable_paper_id(candidate)),
                found=paper_id in index.papers,
                record=index.papers.get(paper_id),
            )
            for candidate in candidates
        )

    async def download_batch(
        self,
        candidates: tuple[PaperCandidate, ...],
        *,
        session_id: str | None,
    ) -> PaperDownloadResult:
        if not candidates:
            raise ValueError("论文候选不能为空")
        paper_ids = tuple(dict.fromkeys(stable_paper_id(item) for item in candidates))
        batch_id = hashlib.sha256(
            f"{datetime.now().astimezone().isoformat()}:{session_id}:{','.join(paper_ids)}:{uuid4().hex}".encode(),
        ).hexdigest()
        grant = PaperBatchGrant(
            batch_id=batch_id,
            session_id=session_id,
            paper_ids=paper_ids,
            created_at=datetime.now().astimezone(),
        )
        # 用户批准下载后先持久化授权，再开始网络与文件副作用。Runtime/Gateway
        # 重启只会重载同一授权，不要求用户为了恢复工作流重复下载。
        async with self.locks.write(self.grants_path):
            grants = self._read_grants()
            grants.grants[batch_id] = grant
            self._write_grants(grants)
            self._grants[batch_id] = grant
        items: list[PaperDownloadItem] = []
        for candidate in candidates:
            items.append(await self._download_one(candidate))
        return PaperDownloadResult(batch_id=batch_id, items=tuple(items))

    async def _download_one(self, candidate: PaperCandidate) -> PaperDownloadItem:
        from tools.download_paper import (
            PaperDownloadNetworkError,
            PaperDownloadSecurityError,
            PaperDownloadServiceError,
        )

        paper_id = stable_paper_id(candidate)
        now = datetime.now().astimezone()
        async with self.locks.write(self.index_path):
            index = self._read_index()
            existing = index.papers.get(paper_id)
            if existing is not None and existing.pdf_path:
                path = self._record_pdf(existing)
                if path.is_file() and existing.sha256 == _sha256_file(path):
                    duplicate = existing.model_copy(update={
                        "status": "summarized" if existing.summary_path else "duplicate",
                        "last_attempt_at": now,
                        "error": None,
                        "retryable": False,
                    })
                    index.papers[paper_id] = duplicate
                    self._write_index(index)
                    return PaperDownloadItem(
                        paper_id=paper_id,
                        status="duplicate",
                        title=existing.title,
                        path=existing.pdf_path,
                    )

        try:
            body, final_url, content_type = await self.downloader.download_bytes(candidate.pdf_url)
        except (PaperDownloadSecurityError, PaperDownloadServiceError, PaperDownloadNetworkError) as exc:
            retryable = isinstance(exc, PaperDownloadNetworkError)
            failed = self._record_from_candidate(
                candidate,
                status="unavailable",
                last_attempt_at=now,
                error=f"{type(exc).__name__}: {str(exc)[:1000]}",
                retryable=retryable,
            )
            async with self.locks.write(self.index_path):
                index = self._read_index()
                previous = index.papers.get(paper_id)
                index.papers[paper_id] = _merge_record(previous, failed)
                self._write_index(index)
            return PaperDownloadItem(
                paper_id=paper_id,
                status="unavailable",
                title=candidate.title,
                error=failed.error,
            )

        digest = hashlib.sha256(body).hexdigest()
        async with self.locks.workspace_exclusive():
            index = self._read_index()
            for other_id, other in index.papers.items():
                if other_id != paper_id and other.sha256 == digest and other.pdf_path:
                    duplicate = self._record_from_candidate(
                        candidate,
                        status="summarized" if other.summary_path else "duplicate",
                        duplicate_of=other_id,
                        stem=other.stem,
                        directory=other.directory,
                        pdf_path=other.pdf_path,
                        last_attempt_at=now,
                        downloaded_at=other.downloaded_at,
                        summary_path=other.summary_path,
                        summarized_at=other.summarized_at,
                        page_count=other.page_count,
                        pages_read=other.pages_read,
                        reference_paper_id=other.reference_paper_id,
                        reference_passage_ids=other.reference_passage_ids,
                        reference_citation_example_ids=other.reference_citation_example_ids,
                        sha256=digest,
                        size_bytes=len(body),
                        final_url=final_url,
                        content_type=content_type,
                    )
                    index.papers[paper_id] = duplicate
                    self._write_index(index)
                    return PaperDownloadItem(
                        paper_id=paper_id,
                        status="duplicate",
                        title=candidate.title,
                        duplicate_of=other_id,
                        path=other.pdf_path,
                    )

            previous = index.papers.get(paper_id)
            stem = previous.stem if previous is not None else self._unique_stem(candidate, paper_id, index)
            directory = self.root / stem
            if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
                raise PermissionError(f"论文目录不是安全的普通目录：{stem}")
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{stem}.pdf"
            if target.exists() and target.is_symlink():
                raise PermissionError("论文 PDF 不能是符号链接")
            _atomic_write_bytes(target, body)
            relative = target.relative_to(self.root).as_posix()
            record = self._record_from_candidate(
                candidate,
                status="downloaded",
                stem=stem,
                directory=stem,
                pdf_path=relative,
                final_url=final_url,
                last_attempt_at=now,
                downloaded_at=now,
                content_type=content_type,
                size_bytes=len(body),
                sha256=digest,
                error=None,
                retryable=False,
            )
            index.papers[paper_id] = _merge_record(previous, record)
            self._write_index(index)
        return PaperDownloadItem(
            paper_id=paper_id,
            status="downloaded",
            title=candidate.title,
            path=record.pdf_path,
        )

    async def read(
        self,
        paper_id: str,
        *,
        start_page: int | None = None,
        end_page: int | None = None,
        offset_chars: int = 0,
        max_chars: int = 30_000,
    ) -> str:
        record = await self.get(paper_id)
        if record.pdf_path is None:
            raise FileNotFoundError("论文尚未下载，不能读取全文")
        path = self._record_pdf(record)
        return await self.reader.read_path(
            path,
            display_path=f"papers/{record.pdf_path}",
            arguments={
                "start_page": start_page,
                "end_page": end_page,
                "offset_chars": offset_chars,
                "max_chars": max_chars,
            },
            file_locks=self.locks,
        )

    async def save(self, update: PaperSummaryUpdate, *, session_id: str | None) -> PaperRecord:
        self.require_grant(update.batch_id, update.paper_id, session_id)
        async with self.locks.workspace_exclusive():
            index = self._read_index()
            record = index.papers.get(update.paper_id)
            if record is None:
                raise KeyError(f"未知论文：{update.paper_id}")
            if not record.pdf_path or not self._record_pdf(record).is_file():
                raise FileNotFoundError("论文 PDF 尚未成功下载，不能保存解析或总结状态")
            if update.status == "summarized" and record.summary_path:
                existing_summary = self.root / record.summary_path
                if existing_summary.is_file() and not existing_summary.is_symlink():
                    return record
            values: dict[str, Any] = {
                "status": update.status,
                "page_count": update.page_count,
                "pages_read": update.pages_read,
                "ocr_required": update.status == "ocr_required",
                "error": update.error,
                "retryable": update.status == "parse_failed",
            }
            if update.status == "summarized":
                assert update.markdown is not None
                directory = self.root / record.directory
                summary = directory / f"{record.stem}.md"
                _atomic_write_text(summary, update.markdown.rstrip() + "\n")
                values.update({
                    "summary_path": summary.relative_to(self.root).as_posix(),
                    "summarized_at": datetime.now().astimezone(),
                })
            if update.reference_paper_id:
                values["reference_paper_id"] = update.reference_paper_id
            if update.reference_passage_ids:
                values["reference_passage_ids"] = tuple(dict.fromkeys(
                    (*record.reference_passage_ids, *update.reference_passage_ids),
                ))
            if update.reference_citation_example_ids:
                values["reference_citation_example_ids"] = tuple(dict.fromkeys(
                    (*record.reference_citation_example_ids, *update.reference_citation_example_ids),
                ))
            result = record.model_copy(update=values)
            index.papers[update.paper_id] = result
            self._write_index(index)
            return result

    async def get(self, paper_id: str) -> PaperRecord:
        _validate_id(paper_id)
        async with self.locks.read(self.index_path):
            record = self._read_index().papers.get(paper_id)
        if record is None:
            raise KeyError(f"未知论文：{paper_id}")
        return record

    def grant_allows(self, batch_id: str, paper_id: str, session_id: str | None) -> bool:
        # grants.json 使用原子替换，允许不同 Runtime/进程看到其他实例刚创建的批次。
        grants = self._read_grants().grants
        self._grants = dict(grants)
        grant = grants.get(batch_id)
        return bool(
            grant is not None
            and paper_id in grant.paper_ids
            and grant.session_id == session_id
        )

    def require_grant(self, batch_id: str, paper_id: str, session_id: str | None) -> None:
        if not self.grant_allows(batch_id, paper_id, session_id):
            raise PermissionError("论文批次授权无效、已过期或不包含当前论文")

    async def record_reference(
        self,
        paper_id: str,
        *,
        kind: str,
        reference_id: str,
    ) -> None:
        async with self.locks.write(self.index_path):
            index = self._read_index()
            record = index.papers.get(paper_id)
            if record is None:
                raise KeyError(f"未知论文：{paper_id}")
            if kind == "paper":
                update = {"reference_paper_id": reference_id}
            elif kind == "passage":
                update = {"reference_passage_ids": tuple(dict.fromkeys(
                    (*record.reference_passage_ids, reference_id),
                ))}
            elif kind == "citation_example":
                update = {"reference_citation_example_ids": tuple(dict.fromkeys(
                    (*record.reference_citation_example_ids, reference_id),
                ))}
            else:
                raise ValueError(f"未知 Reference 关联类型：{kind}")
            index.papers[paper_id] = record.model_copy(update=update)
            self._write_index(index)

    def reference_paper_id(self, paper_id: str) -> str | None:
        """返回论文库记录已关联的 Reference paper_id。"""
        record = self._read_index().papers.get(paper_id)
        return record.reference_paper_id if record is not None else None

    async def reference_file(self, paper_id: str) -> tuple[PaperRecord, Path]:
        record = await self.get(paper_id)
        if not record.pdf_path:
            raise FileNotFoundError("论文库记录没有 PDF")
        path = self._record_pdf(record)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("论文库 PDF 不存在或不是普通文件")
        return record, path

    def _record_pdf(self, record: PaperRecord) -> Path:
        if not record.pdf_path:
            raise FileNotFoundError("论文记录没有 PDF 路径")
        path = (self.root / Path(record.pdf_path)).resolve()
        if self.root.resolve() not in path.parents:
            raise PermissionError("论文索引中的 PDF 路径越界")
        directory = self.root / record.directory
        if self.root.is_symlink() or directory.is_symlink() or path.is_symlink():
            raise PermissionError("论文库路径不能经过符号链接")
        return path

    def _record_from_candidate(self, candidate: PaperCandidate, **updates: Any) -> PaperRecord:
        paper_id = stable_paper_id(candidate)
        stem = str(updates.pop("stem", sanitize_paper_title(candidate.title)))
        directory = str(updates.pop("directory", stem))
        return PaperRecord(
            paper_id=paper_id,
            title=candidate.title,
            stem=stem,
            directory=directory,
            source_url=candidate.source_url,
            pdf_url=candidate.pdf_url,
            source=candidate.source,
            query=candidate.query,
            authors=candidate.authors,
            year=candidate.year,
            venue=candidate.venue,
            abstract=candidate.abstract,
            doi=normalize_doi(candidate.doi),
            arxiv_id=normalize_arxiv(candidate.arxiv_id),
            discovered_at=datetime.now().astimezone(),
            **updates,
        )

    def _unique_stem(self, candidate: PaperCandidate, paper_id: str, index: PaperIndex) -> str:
        base = sanitize_paper_title(candidate.title)
        used = {record.stem: key for key, record in index.papers.items()}
        if base not in used or used[base] == paper_id:
            return base
        year = str(candidate.year) if candidate.year is not None else "paper"
        return f"{base} - {year}-{paper_id[:8]}"

    def _read_index(self) -> PaperIndex:
        try:
            return PaperIndex.model_validate_json(self.index_path.read_text(encoding="utf-8"), strict=True)
        except (OSError, ValidationError) as exc:
            raise ValueError(f"论文库索引损坏：{exc}") from exc

    def _write_index(self, index: PaperIndex) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.index_path, index.model_dump_json(indent=2) + "\n")

    def _read_grants(self) -> PaperGrantIndex:
        try:
            return PaperGrantIndex.model_validate_json(
                self.grants_path.read_text(encoding="utf-8"),
                strict=True,
            )
        except (OSError, ValidationError) as exc:
            raise ValueError(f"论文库批次授权索引损坏：{exc}") from exc

    def _write_grants(self, grants: PaperGrantIndex) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.grants_path, grants.model_dump_json(indent=2) + "\n")

    def _recover_legacy_grants(self) -> dict[str, PaperBatchGrant]:
        """从真实成功工具记录迁移旧版内存授权；不信任摘要或模型复述。"""
        session_root = self.agent_root / ".yy" / "memory" / "session"
        if not session_root.is_dir() or session_root.is_symlink():
            return {}
        index = self._read_index()
        recovered: dict[str, PaperBatchGrant] = {}
        filename = re.compile(r"^\d{4}-\d{2}-\d{2}_([0-9a-f]{16})_\d+\.jsonl$")
        for path in session_root.rglob("*.jsonl"):
            if path.is_symlink() or not path.is_file():
                continue
            matched = filename.fullmatch(path.name)
            if matched is None:
                continue
            session_id = matched.group(1)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                grant = self._legacy_grant_from_record(line, session_id, index)
                if grant is not None:
                    recovered[grant.batch_id] = grant
        return recovered

    def _legacy_grant_from_record(
        self,
        line: str,
        session_id: str,
        index: PaperIndex,
    ) -> PaperBatchGrant | None:
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or not (
                record.get("role") == "tool"
                and record.get("name") == "paper_library_download"
                and record.get("status") == "success"
                and isinstance(record.get("operation_id"), str)
            ):
                return None
            result = PaperDownloadResult.model_validate_json(str(record["content"]), strict=True)
            raw_candidates = record.get("arguments", {}).get("candidates")
            if not isinstance(raw_candidates, list):
                return None
            candidates = tuple(_candidate_from_audit(item) for item in raw_candidates)
            expected_ids = tuple(dict.fromkeys(stable_paper_id(item) for item in candidates))
            returned_ids = tuple(item.paper_id for item in result.items)
            if set(expected_ids) != set(returned_ids):
                return None
            eligible = tuple(
                paper_id for paper_id in expected_ids
                if _record_has_local_pdf(self, index.papers.get(paper_id))
            )
            if not eligible:
                return None
            created_at = datetime.fromisoformat(str(record["timestamp"]))
            return PaperBatchGrant(
                batch_id=result.batch_id,
                session_id=session_id,
                paper_ids=eligible,
                created_at=created_at,
            )
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            return None


def stable_paper_id(candidate: PaperCandidate) -> str:
    doi = normalize_doi(candidate.doi)
    arxiv = normalize_arxiv(candidate.arxiv_id)
    if doi:
        identity = f"doi:{doi}"
    elif arxiv:
        identity = f"arxiv:{arxiv}"
    elif candidate.source_url:
        identity = f"url:{normalize_url(candidate.source_url)}"
    else:
        title = re.sub(r"\s+", " ", candidate.title).strip().casefold()
        identity = f"title:{title}:{candidate.year or ''}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _candidate_from_audit(value: Any) -> PaperCandidate:
    if not isinstance(value, dict):
        raise TypeError("论文候选审计记录必须是对象")
    cleaned = {key: item for key, item in value.items() if item is not None}
    cleaned["authors"] = tuple(str(item) for item in cleaned.get("authors", ()))
    return PaperCandidate.model_validate(cleaned, strict=True)


def _record_has_local_pdf(
    service: PaperLibraryService,
    record: PaperRecord | None,
) -> bool:
    if record is None or record.pdf_path is None or record.sha256 is None:
        return False
    try:
        path = service._record_pdf(record)
        return path.is_file() and not path.is_symlink() and _sha256_file(path) == record.sha256
    except (OSError, PermissionError):
        return False


def sanitize_paper_title(value: str) -> str:
    cleaned = _INVALID_FILENAME.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "Untitled Paper"
    if cleaned.casefold() in _RESERVED_WINDOWS:
        cleaned = f"Paper - {cleaned}"
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip(" .")
    return cleaned


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I)
    return cleaned.casefold() or None


def normalize_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", value.strip(), flags=re.I)
    return cleaned.removesuffix(".pdf").casefold() or None


def normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), parsed.query, ""))


def _merge_record(previous: PaperRecord | None, current: PaperRecord) -> PaperRecord:
    if previous is None:
        return current
    values = current.model_dump()
    values["discovered_at"] = previous.discovered_at
    for key in (
        "pdf_path", "summary_path", "downloaded_at", "summarized_at", "sha256",
        "size_bytes", "page_count", "reference_paper_id",
    ):
        if values.get(key) is None and getattr(previous, key) is not None:
            values[key] = getattr(previous, key)
    for key in ("reference_passage_ids", "reference_citation_example_ids", "pages_read"):
        if not values.get(key):
            values[key] = getattr(previous, key)
    if previous.stem:
        values["stem"] = previous.stem
        values["directory"] = previous.directory
    return PaperRecord.model_validate(values, strict=True)


def _validate_id(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("paper_id 必须是 64 位小写 SHA-256")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(body)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
