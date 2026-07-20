"""创建完整的本机 `.yy` 配置、记忆与运行目录。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from memory import MemoryStore


@dataclass(frozen=True)
class InitializationResult:
    """一次启动检查的结果。"""

    yy_dir: Path
    initialized: bool


_REQUIRED_PATHS = (
    "settings.local.json",
    "memory/session/index.json",
    "memory/profile/USER.md",
    "memory/profile/RESEARCH.md",
    "memory/profile/OTHERS.md",
    "memory/profile/index.json",
    ".initialized.json",
)


def initialize_project(project_root: Path) -> Path:
    """初始化完整 `.yy` 目录并返回其路径，不覆盖任何已有用户文件。"""
    yy = project_root / ".yy"
    yy.mkdir(parents=True, exist_ok=True)
    local = yy / "settings.local.json"
    if not local.exists():
        template = Path(__file__).parent / "templates" / "settings.local.json.example"
        local.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    MemoryStore(yy / "memory")
    marker = yy / ".initialized.json"
    if not marker.exists():
        marker.write_text(
            json.dumps({"version": 1, "initialized_at": datetime.now().astimezone().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return yy


def is_project_initialized(project_root: Path) -> bool:
    """判断 `.yy` 初始化标记及首期必要文件是否齐全。"""
    yy = project_root / ".yy"
    return all((yy / relative).is_file() for relative in _REQUIRED_PATHS)


def ensure_project_initialized(project_root: Path) -> InitializationResult:
    """仅在首次运行或必要文件缺失时执行初始化。"""
    root = project_root.resolve()
    yy = root / ".yy"
    if is_project_initialized(root):
        return InitializationResult(yy, False)
    return InitializationResult(initialize_project(root), True)
