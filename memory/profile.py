"""用户长期 Profile Markdown 的初始化和加载服务。"""

from __future__ import annotations

from pathlib import Path


DEFAULT_PROFILE_FILES = ("USER.md", "RESEARCH.md", "OTHERS.md")


class ProfileStore:
    """管理可扩展的用户长期记忆文件集合。"""

    def __init__(self, directory: Path, defaults: tuple[str, ...] = DEFAULT_PROFILE_FILES) -> None:
        self.directory, self.defaults = directory, defaults

    def initialize(self) -> None:
        """首次运行时创建默认 Markdown 文件，绝不覆盖已有内容。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in self.defaults:
            (self.directory / name).touch(exist_ok=True)

    def load_all(self) -> str:
        """读取所有 Markdown Profile，自动包含后续新增的文件。"""
        self.initialize()
        parts = []
        for path in sorted(self.directory.glob("*.md")):
            content = path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[{path.stem}]\n{content}")
        return "\n\n".join(parts)
