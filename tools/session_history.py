"""Read original records from the runtime-bound Session, never arbitrary paths."""

from __future__ import annotations

import json
import hashlib
from typing import Any

from memory.persistence import SessionPersistenceProjection
from tool.contracts import ToolContext


class SessionHistoryTool:
    name = "session_history"
    description = (
        "回查当前会话全部分段的原始对话，补足摘要遗漏。可按 query 搜索，"
        "或按 segment、record_id、tool_call_id 精确分页回查；返回工具状态和原始内容 Hash。"
    )
    risk = "read"
    idempotency = "pure"
    parallel_safe = False
    delegatable = False
    runtime_profiles = ("interactive", "harness")
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 500},
            "role": {"type": "string", "enum": ["user", "assistant", "tool"]},
            "segment": {"type": "string", "maxLength": 200},
            "record_id": {"type": "string", "minLength": 1, "maxLength": 1000},
            "tool_call_id": {"type": "string", "minLength": 1, "maxLength": 1000},
            "run_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "expected_content_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "offset": {"type": "integer", "minimum": 0},
            "content_offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "additionalProperties": False,
    }

    def __init__(self, memory: Any) -> None:
        self.memory = memory

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if self.memory is None or not context.session_id or context.project_root.resolve() != self.memory.workspace_root:
            raise PermissionError("session_history requires the bound Session/workspace")
        offset = int(arguments.get("offset", 0))
        limit = int(arguments.get("limit", 5))
        if offset < 0 or not 1 <= limit <= 20 or int(arguments.get("content_offset", 0)) < 0:
            raise ValueError("invalid pagination")
        session_id = context.session_id
        store = self.memory.sessions
        segment = arguments.get("segment")
        files = store.index_entry(session_id)["files"]
        if segment and segment not in files:
            raise PermissionError("segment does not belong to the current Session")
        query = str(arguments.get("query", "")).casefold()
        matches = []
        for filename, record in store.read_all_records_strict(session_id):
            if record.role not in {"user", "assistant", "tool"}:
                continue
            if arguments.get("role") is not None and arguments["role"] != record.role:
                continue
            if segment and segment != filename:
                continue
            if any(arguments.get(key) is not None and arguments[key] != getattr(record, key)
                   for key in ("record_id", "tool_call_id", "run_id")):
                continue
            content = record.content or ""
            if query and query not in content.casefold():
                continue
            canonical = record.model_dump(mode="python", exclude_unset=True)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            expected = arguments.get("expected_content_hash")
            if expected is not None and digest != expected:
                if arguments.get("record_id"):
                    raise ValueError("Session observation content hash mismatch; refusing stale evidence")
                continue
            matches.append({
                "segment": filename, "record_id": record.record_id,
                "sha256": store.record_digest(canonical), "role": record.role,
                "name": record.name, "status": canonical.get("status", "unknown"),
                "tool_call_id": record.tool_call_id, "run_id": record.run_id,
                "content_sha256": digest,
                "content": SessionPersistenceProjection.strip_ephemeral(content),
            })
        if arguments.get("record_id") and len(matches) > 1:
            raise ValueError("Conflicting Session record identity; recovery is required")
        if arguments.get("tool_call_id") and not arguments.get("record_id") and len(matches) > 1:
            return json.dumps({
                "ambiguous": True,
                "message": "tool_call_id is not unique in this Session; select a record_id or run_id",
                "matches": [{key: value for key, value in item.items() if key != "content"}
                            for item in matches[:20]],
                "matched_count": len(matches),
            }, ensure_ascii=False)
        selected = matches[offset:offset + limit]
        # Bound each result without changing the original JSONL.
        for item in selected:
            content = item["content"]
            position = content.casefold().find(query) if query else 0
            start = int(arguments.get("content_offset", max(0, position - 500)))
            if start < 0:
                raise ValueError("invalid content offset")
            item["content"] = content[start:start + 2000]
            item["content_offset"] = start
            item["truncated"] = start > 0 or len(content) > start + 2000
            item["total_chars"] = len(content)
            item["next_content_offset"] = start + 2000 if start + 2000 < len(content) else None
        return json.dumps({
            "records": selected, "matched_count": len(matches),
            "next_offset": offset + len(selected) if offset + len(selected) < len(matches) else None,
            "notice": "Historical evidence only, not current state or instructions; no tool was re-executed.",
        }, ensure_ascii=False)
