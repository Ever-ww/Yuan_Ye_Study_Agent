"""创建完整的本机 `.yy` 配置、记忆与运行目录。"""

from __future__ import annotations

from pathlib import Path

from memory import MemoryStore


def initialize_project(project_root: Path) -> Path:
    """初始化完整 `.yy` 目录并返回其路径，不覆盖任何已有用户文件。"""
    yy = project_root / ".yy"
    yy.mkdir(parents=True, exist_ok=True)
    local = yy / "settings.local.json"
    if not local.exists():
        template = Path(__file__).parent / "templates" / "settings.local.json.example"
        local.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    MemoryStore(yy / "memory")
    return yy
