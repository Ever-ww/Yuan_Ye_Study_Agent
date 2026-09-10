"""Read exact canonical records from one file of the runtime-bound Session."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from memory.persistence import SessionPersistenceProjection
from tool.contracts import ToolContext


class SessionReadTool:
    """Recover full content referenced by a historical Tool-output projection."""

    name = "session_read"
    description = (
        "读取当前Runtime绑定会话中某个JSONL文件的原始记录。"
        "session_id填写裁剪提示给出的会话文件名，record_id填写记录标识；"
        "offset从该记录向后偏移，limited控制返回几条，默认只返回该记录。"
    )
    risk = "read"
    idempotency = "pure"
    parallel_safe = False
    delegatable = False
    runtime_profiles = ("interactive", "harness")
    schema = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "当前会话索引中的JSONL文件名，不是任意路径",
            },
            "record_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "description": "该Session JSONL行的稳定record_id",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "以record_id为基准向后偏移的可见记录数",
            },
            "limited": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 1,
                "description": "从偏移位置读取的记录数，默认仅一条",
            },
        },
        "required": ["session_id", "record_id"],
        "additionalProperties": False,
    }

    def __init__(self, memory: Any) -> None:
        self.memory = memory

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if (
            self.memory is None
            or not context.session_id
            or context.project_root.resolve() != self.memory.workspace_root
        ):
            raise PermissionError("session_read requires the runtime-bound Session/workspace")

        filename = str(arguments["session_id"])
        record_id = str(arguments["record_id"])
        offset = int(arguments.get("offset", 0))
        limited = int(arguments.get("limited", 1))
        if offset < 0 or not 1 <= limited <= 20:
            raise ValueError("invalid session_read range")

        store = self.memory.sessions
        indexed_files = store.index_entry(context.session_id)["files"]
        if filename not in indexed_files:
            raise PermissionError("session_id does not name a file in the current bound Session")

        readable: list[dict[str, Any]] = []
        base_indexes: list[int] = []
        for selected_file, record in store.read_all_records_strict(context.session_id):
            if selected_file != filename or record.role not in {
                "user", "assistant", "tool", "summary",
            }:
                continue
            canonical = record.model_dump(mode="python", exclude_unset=True)
            content = SessionPersistenceProjection.strip_ephemeral(record.content or "")
            readable.append({
                "session_id": selected_file,
                "record_id": record.record_id,
                "role": record.role,
                "name": record.name,
                "status": canonical.get("status", "unknown"),
                "tool_call_id": record.tool_call_id,
                "run_id": record.run_id,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content": content,
            })
            if record.record_id == record_id:
                base_indexes.append(len(readable) - 1)

        if not base_indexes:
            raise KeyError("record_id was not found in the selected Session file")
        if len(base_indexes) != 1:
            raise ValueError("Conflicting Session record identity; recovery is required")
        start = base_indexes[0] + offset
        selected = readable[start:start + limited]
        return json.dumps({
            "records": selected,
            "base_record_id": record_id,
            "offset": offset,
            "limited": limited,
            "returned": len(selected),
            "notice": (
                "Canonical historical Session evidence only; this does not re-execute the Tool "
                "and does not prove current external state."
            ),
        }, ensure_ascii=False)
