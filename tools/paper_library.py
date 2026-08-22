"""供模型使用的全局论文库查重、批量下载、分页读取与总结写入工具。"""

from __future__ import annotations

import json
from typing import Any

from paper_library import PaperCandidate, PaperLibraryService, PaperSummaryUpdate
from tool.contracts import ToolContext


_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 2000},
        "pdf_url": {"type": "string", "minLength": 1, "maxLength": 4000},
        "source_url": {"type": "string", "maxLength": 4000},
        "authors": {"type": "array", "items": {"type": "string"}, "maxItems": 200},
        "year": {"type": "integer", "minimum": 1000, "maximum": 3000},
        "venue": {"type": "string"},
        "abstract": {"type": "string"},
        "doi": {"type": "string", "maxLength": 512},
        "arxiv_id": {"type": "string", "maxLength": 128},
        "query": {"type": "string"},
        "source": {"type": "string"},
    },
    "required": ["title", "pdf_url"],
}


class PaperLibraryLookupTool:
    name = "paper_library_lookup"
    description = "在下载前按 DOI、arXiv ID、规范 URL 或题名年份查询全局论文索引并去重"
    risk = "read"
    parallel_safe = True
    schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": _CANDIDATE_SCHEMA,
                "minItems": 1,
                "maxItems": 50,
            },
        },
        "required": ["candidates"],
    }

    def __init__(self, service: PaperLibraryService) -> None:
        self.service = service

    def prepare_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _clean_candidate_arguments(arguments)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        candidates = _candidates(arguments["candidates"])
        values = await self.service.lookup(candidates)
        return json.dumps(
            [item.model_dump(mode="json") for item in values],
            ensure_ascii=False,
            indent=2,
        )


class PaperLibraryDownloadTool:
    name = "paper_library_download"
    description = (
        "经一次用户审批后，把一批已核对的公开 PDF 下载到全局论文库；"
        "逐篇记录成功、重复和失败状态，不绕过登录、验证码或付费墙"
    )
    risk = "write"
    schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": _CANDIDATE_SCHEMA,
                "minItems": 1,
                "maxItems": 20,
            },
        },
        "required": ["candidates"],
    }

    def __init__(self, service: PaperLibraryService) -> None:
        self.service = service

    def prepare_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _clean_candidate_arguments(arguments)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        result = await self.service.download_batch(
            _candidates(arguments["candidates"]),
            session_id=context.session_id,
        )
        return result.model_dump_json(indent=2)


class PaperLibraryReadTool:
    name = "paper_library_read"
    description = "按论文库 paper_id 分页读取已下载 PDF 的文字；扫描件会明确返回 ocr_required"
    risk = "read"
    parallel_safe = True
    schema = {
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "start_page": {"type": "integer", "minimum": 1},
            "end_page": {"type": "integer", "minimum": 1},
            "offset_chars": {"type": "integer", "minimum": 0, "maximum": 5_000_000},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50_000},
        },
        "required": ["paper_id"],
    }

    def __init__(self, service: PaperLibraryService) -> None:
        self.service = service

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        return await self.service.read(
            str(arguments["paper_id"]),
            start_page=arguments.get("start_page"),
            end_page=arguments.get("end_page"),
            offset_chars=int(arguments.get("offset_chars") or 0),
            max_chars=int(arguments.get("max_chars") or 30_000),
        )


class PaperLibrarySaveTool:
    name = "paper_library_save"
    description = (
        "保存论文中文总结或记录 parse_failed/ocr_required；只能使用本次已批准批次的 batch_id"
    )
    risk = "write"
    schema = {
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "batch_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "status": {"type": "string", "enum": ["summarized", "parse_failed", "ocr_required"]},
            "markdown": {"type": "string"},
            "page_count": {"type": "integer", "minimum": 0},
            "pages_read": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "maxItems": 10000,
            },
            "error": {"type": "string"},
            "reference_paper_id": {"type": "string"},
            "reference_passage_ids": {"type": "array", "items": {"type": "string"}},
            "reference_citation_example_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["paper_id", "batch_id", "status"],
    }

    def __init__(self, service: PaperLibraryService) -> None:
        self.service = service

    def approval_required(self, arguments: dict[str, Any], context: ToolContext) -> bool:
        return not self.service.grant_allows(
            str(arguments.get("batch_id", "")),
            str(arguments.get("paper_id", "")),
            context.session_id,
        )

    def ensure_available(self, arguments: dict[str, Any], context: ToolContext) -> None:
        self.service.require_grant(
            str(arguments.get("batch_id", "")),
            str(arguments.get("paper_id", "")),
            context.session_id,
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        update = PaperSummaryUpdate(
            paper_id=str(arguments["paper_id"]),
            batch_id=str(arguments["batch_id"]),
            status=str(arguments["status"]),
            markdown=arguments.get("markdown"),
            page_count=arguments.get("page_count"),
            pages_read=tuple(arguments.get("pages_read") or ()),
            error=arguments.get("error"),
            reference_paper_id=arguments.get("reference_paper_id"),
            reference_passage_ids=tuple(arguments.get("reference_passage_ids") or ()),
            reference_citation_example_ids=tuple(
                arguments.get("reference_citation_example_ids") or (),
            ),
        )
        return (await self.service.save(update, session_id=context.session_id)).model_dump_json(indent=2)


def _candidates(values: Any) -> tuple[PaperCandidate, ...]:
    if not isinstance(values, list):
        raise ValueError("candidates 必须是数组")
    prepared = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("每个论文候选必须是对象")
        cleaned = {key: item for key, item in value.items() if item is not None}
        cleaned["authors"] = tuple(str(item) for item in cleaned.get("authors", ()))
        prepared.append(PaperCandidate.model_validate(cleaned, strict=True))
    return tuple(prepared)


def _clean_candidate_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    values = arguments.get("candidates")
    if not isinstance(values, list):
        return dict(arguments)
    return {
        **arguments,
        "candidates": [
            {key: item for key, item in value.items() if item is not None}
            if isinstance(value, dict)
            else value
            for value in values
        ],
    }
