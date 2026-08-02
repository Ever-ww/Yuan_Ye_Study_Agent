"""参考 PI Agent 语义实现的单文件精确多块编辑工具。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from tool.contracts import ToolContext
from tool.path_guard import safe_workspace_path


class EditBlock(BaseModel):
    """一处基于原始文件内容定位的精确替换。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    oldText: str
    newText: str


class EditTool:
    """对已有文件执行互不重叠的精确替换，并创建一次 checkpoint。"""

    name = "edit"
    description = (
        "使用精确文本替换编辑单个已有文件。edits 中每个 oldText 必须在原文件中唯一；"
        "多个替换都基于修改前的原文件且不得重叠，相邻修改应合并为一个替换块。"
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的已有文件路径",
            },
            "edits": {
                "type": "array",
                "minItems": 1,
                "description": "一个或多个基于原始文件的互不重叠精确替换",
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {"type": "string"},
                        "newText": {"type": "string"},
                    },
                    "required": ["oldText", "newText"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    }
    risk = "write"

    def prepare_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """兼容 PI 对常见模型参数偏差的规范化，再交给正式 Schema 校验。"""
        prepared = dict(arguments)
        path = prepared.get("path")
        if isinstance(path, str) and path.startswith("@"):
            prepared["path"] = path[1:]
        edits = prepared.get("edits")
        if isinstance(edits, str):
            try:
                parsed = json.loads(edits)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                prepared["edits"] = parsed

        old_text = prepared.get("oldText")
        new_text = prepared.get("newText")
        if isinstance(old_text, str) and isinstance(new_text, str):
            current = prepared.get("edits")
            prepared["edits"] = [
                *(current if isinstance(current, list) else []),
                {"oldText": old_text, "newText": new_text},
            ]
            prepared.pop("oldText", None)
            prepared.pop("newText", None)
        return prepared

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 checkpoint，禁止执行 edit")
        if context.file_locks is None:
            raise RuntimeError("当前 Runtime 未启用文件锁，禁止执行 edit")

        path = safe_workspace_path(context.project_root, arguments["path"])
        blocks = _validate_blocks(arguments.get("edits"))
        async with context.file_locks.write(path):
            if not path.is_file():
                raise FileNotFoundError(f"无法编辑不存在的文件：{arguments['path']}")
            if not os.access(path, os.R_OK | os.W_OK):
                raise PermissionError(f"文件不可读写：{arguments['path']}")

            raw = path.read_bytes()
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"edit 只支持 UTF-8 文本文件：{arguments['path']}",
                ) from exc

            bom, content = ("\ufeff", decoded[1:]) if decoded.startswith("\ufeff") else ("", decoded)
            line_ending = _detect_line_ending(content)
            original = _normalize_line_endings(content)
            replacements = _locate_replacements(original, blocks, arguments["path"])

            updated = original
            for start, end, replacement in reversed(replacements):
                updated = updated[:start] + replacement + updated[end:]
            if updated == original:
                return f"文件内容未变化：{arguments['path']}；未创建 checkpoint"

            final = bom + _restore_line_endings(updated, line_ending)
            _atomic_replace(path, final)
            try:
                checkpoint_edit = getattr(context.sandbox, "checkpoint_edit", None)
                checkpoint = (
                    await checkpoint_edit(arguments["path"])
                    if callable(checkpoint_edit)
                    else await context.sandbox.checkpoint_write(arguments["path"])
                )
            except Exception:
                await context.sandbox.restore_current()
                raise

            if checkpoint is None:
                return (
                    f"已精确替换 {len(blocks)} 处：{arguments['path']}；"
                    "工作区无可提交变化"
                )
            return (
                f"已精确替换 {len(blocks)} 处：{arguments['path']}；"
                f"checkpoint {checkpoint.commit_sha}"
            )


def _validate_blocks(value: Any) -> tuple[EditBlock, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("edit 的 edits 必须至少包含一个替换块")
    try:
        blocks = tuple(EditBlock.model_validate(item) for item in value)
    except ValidationError as exc:
        raise ValueError(f"edit 替换块校验失败：{exc}") from exc
    if any(not block.oldText for block in blocks):
        raise ValueError("edit 的 oldText 不能为空")
    return blocks


def _locate_replacements(
    original: str,
    blocks: tuple[EditBlock, ...],
    display_path: str,
) -> list[tuple[int, int, str]]:
    locations: list[tuple[int, int, str]] = []
    for block in blocks:
        old = _normalize_line_endings(block.oldText)
        new = _normalize_line_endings(block.newText)
        start = original.find(old)
        if start < 0:
            raise ValueError(f"oldText 在文件中不存在：{display_path}")
        if original.find(old, start + 1) >= 0:
            raise ValueError(f"oldText 在文件中不是唯一匹配：{display_path}")
        locations.append((start, start + len(old), new))

    ordered = sorted(locations, key=lambda item: item[0])
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError(f"edit 替换块存在重叠或嵌套：{display_path}")
    return ordered


def _detect_line_ending(value: str) -> str:
    if "\r\n" in value:
        return "\r\n"
    if "\r" in value:
        return "\r"
    return "\n"


def _normalize_line_endings(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _restore_line_endings(value: str, line_ending: str) -> str:
    return value if line_ending == "\n" else value.replace("\n", line_ending)


def _atomic_replace(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        # 必须按字节写入；Windows 文本模式会把已恢复的 CRLF 再转换为 CRCRLF。
        temporary.write_bytes(content.encode("utf-8"))
        try:
            temporary.chmod(path.stat().st_mode)
        except OSError:
            pass
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["EditBlock", "EditTool"]
