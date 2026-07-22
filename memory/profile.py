"""用户长期 Profile Markdown 的初始化和加载服务。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ProfileIndex


DEFAULT_PROFILE_FILES = ("USER.md", "RESEARCH.md", "OTHERS.md")


class ProfileStore:
    """管理可扩展的用户长期记忆文件集合。"""

    def __init__(self, directory: Path, defaults: tuple[str, ...] = DEFAULT_PROFILE_FILES) -> None:
        self.directory, self.defaults = directory, defaults
        self.index_path = directory / "index.json"

    def initialize(self) -> None:
        """首次运行时创建默认 Markdown 文件，绝不覆盖已有内容。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in self.defaults:
            (self.directory / name).touch(exist_ok=True)
        if not self.index_path.exists():
            self._write_index({"version": 1, "profiles": {}})

    def load_all(self) -> str:
        """读取所有 Markdown Profile，自动包含后续新增的文件。"""
        return self.load_for_session(None)

    def load_for_session(self, session_id: str | None) -> str:
        """加载全局 Profile 与当前 Session Profile，隔离其他会话哈希。"""
        self.initialize()
        parts = []
        paths = sorted(self.directory.glob("*.md"))
        if session_id is not None:
            session_path = self.directory / f"{session_id}.md"
            paths = ([session_path] if session_path in paths else []) + [path for path in paths if path != session_path]
        for path in paths:
            if re.fullmatch(r"[0-9a-f]{16}", path.stem) and path.stem != session_id:
                continue
            content = path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[{path.stem}]\n{content}")
        return "\n\n".join(parts)

    def session_profile(self, session_id: str) -> str:
        """读取单个会话哈希对应的合并 Profile。"""
        path = self.directory / f"{session_id}.md"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def commit_session_profile(
        self,
        session_id: str,
        markdown: str,
        *,
        source_file: str,
        conversation_turns: int,
        records_processed: int,
        tool_calls_processed: int,
    ) -> Path:
        """原子更新哈希 Profile，并累计其上下文整理索引。"""
        self.initialize()
        profile_path = self.directory / f"{session_id}.md"
        temporary = profile_path.with_suffix(".md.tmp")
        temporary.write_text(markdown.strip() + "\n", encoding="utf-8")
        temporary.replace(profile_path)
        index = self._read_index()
        current = index["profiles"].setdefault(session_id, {
            "file": profile_path.name,
            "source_files": [],
            "segments_processed": 0,
            "conversation_turns": 0,
            "records_processed": 0,
            "tool_calls_processed": 0,
        })
        if source_file not in current["source_files"]:
            current["source_files"].append(source_file)
            current["segments_processed"] += 1
            current["conversation_turns"] += conversation_turns
            current["records_processed"] += records_processed
            current["tool_calls_processed"] += tool_calls_processed
        current["last_updated_at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self._write_index(index)
        return profile_path

    def _read_index(self) -> dict[str, Any]:
        self.initialize()
        return ProfileIndex.model_validate_json(
            self.index_path.read_text(encoding="utf-8"), strict=True,
        ).model_dump(mode="python")

    def _write_index(self, value: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        validated = ProfileIndex.model_validate(value, strict=True)
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(validated.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.index_path)
