"""工作区文本搜索工具。"""

from typing import Any

from .contracts import ToolContext


class SearchWorkspaceTool:
    """在非运行产物目录中执行数量受限的文本搜索。"""

    name = "search_workspace"
    description = "搜索工作区文本"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    risk = "read"
    _excluded_directories = {".git", ".yy", ".venv", "__pycache__"}

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        query = arguments["query"].lower()
        matches: list[str] = []
        root = context.project_root.resolve()
        for path in root.rglob("*"):
            if len(matches) >= 30:
                break
            if not path.is_file() or self._excluded_directories.intersection(path.relative_to(root).parts):
                continue
            try:
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if query in line.lower():
                        matches.append(f"{path.relative_to(root)}:{number}: {line[:200]}")
                        if len(matches) >= 30:
                            break
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(matches) or "未找到匹配内容"
