"""受控读取 Agent Home 中的长期 Profile Markdown。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tool.contracts import ToolContext


class ProfileReadTool:
    name = "profile_read"
    description = "读取全局长期记忆中的指定 Profile Markdown，例如 RESEARCH、USER 或 OTHERS"
    risk = "read"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                "description": "不含扩展名的 Profile 名称，例如 RESEARCH",
            },
        },
        "required": ["name"],
    }

    def __init__(self, agent_root: Path) -> None:
        self.root = agent_root.resolve() / ".yy" / "memory" / "profile"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        del context
        name = str(arguments["name"]).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("Profile 名称格式无效")
        if self.root.is_symlink():
            raise PermissionError("Profile 根目录不能是符号链接")
        root = self.root.resolve()
        path = root / f"{name}.md"
        if path.is_symlink():
            raise PermissionError("profile_read 不允许读取符号链接")
        resolved = path.resolve()
        if resolved.parent != root:
            raise PermissionError("Profile 路径越界")
        if not resolved.is_file():
            raise FileNotFoundError(f"Profile 不存在：{name}.md")
        if resolved.suffix.casefold() != ".md":
            raise PermissionError("profile_read 只允许读取 Markdown")
        return resolved.read_text(encoding="utf-8")
