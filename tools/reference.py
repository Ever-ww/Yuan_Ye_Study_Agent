"""论文 Reference 资料库的只读检索与受审批写入工具。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from reference import (
    Author,
    CitationExampleCreate,
    PaperIdentifier,
    PaperUpsert,
    ReferenceSearchRequest,
    ReferenceService,
    SourcePassageCreate,
)
from reference.models import PaperFile
from tool.contracts import ToolContext
from tool.path_guard import safe_workspace_path

if TYPE_CHECKING:
    from paper_library import PaperLibraryService


class ReferenceSearchTool:
    name = "reference_search"
    description = "检索全局论文资料库中的论文、可核验原文摘录与写作引用例句；支持全文和语义混合检索"
    risk = "read"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "mode": {"type": "string", "enum": ["rrf", "weighted", "separate"]},
            "entity_types": {"type": "array", "items": {"type": "string", "enum": ["paper", "passage", "citation_example"]}},
            "author": {"type": "string"}, "year_from": {"type": "integer"}, "year_to": {"type": "integer"},
            "tag": {"type": "string"}, "language": {"type": "string"}, "top_k": {"type": "integer"},
        },
        "required": ["query"],
    }

    def __init__(self, service: ReferenceService, default_mode: str = "rrf") -> None:
        self.service = service
        self.default_mode = default_mode

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        values = {key: value for key, value in arguments.items() if value is not None}
        values["mode"] = values.get("mode") or self.default_mode
        if "entity_types" in values:
            values["entity_types"] = tuple(values["entity_types"])
        request = ReferenceSearchRequest.model_validate(values, strict=True)
        return (await self.service.search(request)).model_dump_json(indent=2)


class ReferenceGetTool:
    name = "reference_get"
    description = "按稳定 ID 读取论文完整资料、原文摘录或写作引用例句"
    risk = "read"
    schema = {
        "type": "object",
        "properties": {
            "entity_type": {"type": "string", "enum": ["paper", "passage", "citation_example"]},
            "entity_id": {"type": "string", "minLength": 1},
        },
        "required": ["entity_type", "entity_id"],
    }

    def __init__(self, service: ReferenceService) -> None:
        self.service = service

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        return json.dumps(
            self.service.get(str(arguments["entity_type"]), str(arguments["entity_id"])),
            ensure_ascii=False,
            indent=2,
        )


class ReferenceWriteTool:
    name = "reference_write"
    description = (
        "经用户审批后维护全局论文资料库：新增或更新论文、保存原文摘录、保存写作引用例句、"
        "关联工作区 PDF、归档/恢复论文或重新生成向量"
    )
    risk = "write"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "upsert_paper", "add_passage", "add_citation_example", "link_file",
                "archive", "restore", "reembed",
            ]},
            "paper": {"type": "object"},
            "passage": {"type": "object"},
            "citation_example": {"type": "object"},
            "paper_id": {"type": "string"},
            "path": {"type": "string"},
            "scope": {"type": "string", "enum": ["workspace", "paper_library"]},
            "library_paper_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "batch_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["action"],
    }

    def __init__(
        self,
        service: ReferenceService,
        paper_library: "PaperLibraryService | None" = None,
    ) -> None:
        self.service = service
        self.paper_library = paper_library

    def approval_required(self, arguments: dict[str, Any], context: ToolContext) -> bool:
        batch_id = arguments.get("batch_id")
        library_paper_id = arguments.get("library_paper_id")
        if not isinstance(batch_id, str) or not isinstance(library_paper_id, str):
            return True
        return not self._grant_allows(batch_id, library_paper_id, context)

    def ensure_available(self, arguments: dict[str, Any], context: ToolContext) -> None:
        batch_id = arguments.get("batch_id")
        library_paper_id = arguments.get("library_paper_id")
        scope = arguments.get("scope", "workspace")
        if batch_id is not None or library_paper_id is not None or scope == "paper_library":
            if not isinstance(batch_id, str) or not isinstance(library_paper_id, str):
                raise PermissionError("论文库 Reference 写入必须同时提供 batch_id 和 library_paper_id")
            if not self._grant_allows(batch_id, library_paper_id, context):
                raise PermissionError("论文批次授权无效，不能写入 Reference")

    def _grant_allows(self, batch_id: str, paper_id: str, context: ToolContext) -> bool:
        return bool(
            self.paper_library is not None
            and self.paper_library.grant_allows(batch_id, paper_id, context.session_id)
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        action = str(arguments["action"])
        if action == "upsert_paper":
            value = _paper(arguments.get("paper"), context)
            result = self.service.upsert_paper(value)
            await self._record_reference(arguments, "paper", result.paper_id)
            return result.model_dump_json(indent=2)
        if action == "add_passage":
            value = _passage(arguments.get("passage"), context)
            result = self.service.add_passage(value)
            await self._record_reference(arguments, "passage", result.passage_id)
            return result.model_dump_json(indent=2)
        if action == "add_citation_example":
            value = _citation_example(arguments.get("citation_example"), context)
            result = self.service.add_citation_example(value)
            await self._record_reference(arguments, "citation_example", result.example_id)
            return result.model_dump_json(indent=2)
        paper_id = _required(arguments, "paper_id")
        if action == "link_file":
            scope = str(arguments.get("scope") or "workspace")
            if scope == "paper_library":
                if self.paper_library is None:
                    raise RuntimeError("当前 Runtime 未启用全局论文库")
                library_id = _required(arguments, "library_paper_id")
                record, path = await self.paper_library.reference_file(library_id)
                async with self.paper_library.locks.read(path):
                    body = path.read_bytes()
                workspace = self.paper_library.root.resolve()
                relative_path = path.relative_to(workspace).as_posix()
                workspace_hash = "global-paper-library"
            else:
                relative_path = _required(arguments, "path")
                path = safe_workspace_path(context.project_root, relative_path)
                if not path.is_file() or path.suffix.casefold() != ".pdf":
                    raise ValueError("reference_write.link_file 只能关联当前 workspace 中存在的 PDF")
                if context.file_locks is not None:
                    async with context.file_locks.read(path):
                        body = path.read_bytes()
                else:
                    body = path.read_bytes()
                workspace = context.project_root.resolve()
                workspace_hash = hashlib.sha256(str(workspace).casefold().encode()).hexdigest()[:16]
            result = self.service.add_file(paper_id, PaperFile(
                workspace_hash=workspace_hash,
                workspace_root=str(workspace), relative_path=relative_path, absolute_path=str(path),
                sha256=hashlib.sha256(body).hexdigest(), mime_type="application/pdf",
                size_bytes=len(body), is_primary=True,
                source_session_id=context.session_id,
            ))
            return result.model_dump_json(indent=2)
        if action == "archive":
            return self.service.archive(paper_id).model_dump_json(indent=2)
        if action == "restore":
            return self.service.restore(paper_id).model_dump_json(indent=2)
        if action == "reembed":
            return json.dumps({"queued": self.service.reembed(paper_id)}, ensure_ascii=False)
        raise ValueError(f"未知 Reference 写入操作：{action}")

    async def _record_reference(
        self,
        arguments: dict[str, Any],
        kind: str,
        reference_id: str,
    ) -> None:
        library_id = arguments.get("library_paper_id")
        if self.paper_library is not None and isinstance(library_id, str):
            await self.paper_library.record_reference(
                library_id,
                kind=kind,
                reference_id=reference_id,
            )


def _paper(value: Any, context: ToolContext) -> PaperUpsert:
    if not isinstance(value, dict):
        raise ValueError("upsert_paper 需要 paper 对象")
    cleaned = {key: item for key, item in value.items() if item is not None}
    cleaned["identifiers"] = tuple(PaperIdentifier.model_validate(item) for item in cleaned.get("identifiers", ()))
    cleaned["authors"] = tuple(Author.model_validate(item) for item in cleaned.get("authors", ()))
    cleaned["tags"] = tuple(str(item) for item in cleaned.get("tags", ()))
    cleaned.setdefault("source_session_id", context.session_id)
    cleaned.setdefault("source_workspace", str(context.project_root.resolve()))
    return PaperUpsert.model_validate(cleaned, strict=True)


def _passage(value: Any, context: ToolContext) -> SourcePassageCreate:
    if not isinstance(value, dict):
        raise ValueError("add_passage 需要 passage 对象")
    cleaned = {key: item for key, item in value.items() if item is not None}
    cleaned.setdefault("source_workspace", str(context.project_root.resolve()))
    cleaned.setdefault("source_session_id", context.session_id)
    return SourcePassageCreate.model_validate(cleaned, strict=True)


def _citation_example(value: Any, context: ToolContext) -> CitationExampleCreate:
    if not isinstance(value, dict):
        raise ValueError("add_citation_example 需要 citation_example 对象")
    cleaned = {key: item for key, item in value.items() if item is not None}
    cleaned["source_passage_ids"] = tuple(str(item) for item in cleaned.get("source_passage_ids", ()))
    cleaned.setdefault("source_workspace", str(context.project_root.resolve()))
    cleaned.setdefault("source_session_id", context.session_id)
    return CitationExampleCreate.model_validate(cleaned, strict=True)


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"当前操作需要 {key}")
    return value.strip()
