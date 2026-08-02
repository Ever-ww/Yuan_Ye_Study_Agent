"""创建完整的本机 `.yy` 配置、记忆与运行目录。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from memory import MemoryStore
from cron import CronState, HeartbeatState


class InitializationResult(BaseModel):
    """一次启动检查的结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    yy_dir: Path
    initialized: bool


_REQUIRED_PATHS = (
    "settings.local.json",
    "memory/session/index.json",
    "memory/profile/USER.md",
    "memory/profile/RESEARCH.md",
    "memory/profile/OTHERS.md",
    "memory/profile/index.json",
    "agents/SOUL.md",
    "agents/AGENT.md",
    "skills/index.json",
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
    memory_required = (
        yy / "memory" / "session" / "index.json",
        yy / "memory" / "profile" / "USER.md",
        yy / "memory" / "profile" / "RESEARCH.md",
        yy / "memory" / "profile" / "OTHERS.md",
        yy / "memory" / "profile" / "index.json",
    )
    if not all(path.exists() for path in memory_required):
        MemoryStore(yy / "memory")
    agents = yy / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    templates = {
        "SOUL.md": "# Agent 身份\n\n你是 Yuan Ye Agent：本地优先、谨慎且透明的学习与研究助手。\n",
        "AGENT.md": "# 项目说明\n\n在这里维护本项目的架构约束、开发规范与运行说明。\n",
    }
    for name, content in templates.items():
        target = agents / name
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    (project_root / "skills").mkdir(parents=True, exist_ok=True)
    for directory in ("review", "audit", "backups"):
        (yy / "skills" / directory).mkdir(parents=True, exist_ok=True)
    for directory in ("runs",):
        (yy / "gateway" / directory).mkdir(parents=True, exist_ok=True)
    cron_directory = yy / "cron"
    cron_directory.mkdir(parents=True, exist_ok=True)
    cron_state = cron_directory / "jobs.json"
    if not cron_state.exists():
        cron_state.write_text(
            CronState(heartbeat=HeartbeatState()).model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    skill_index = yy / "skills" / "index.json"
    if not skill_index.exists():
        skill_index.write_text(
            json.dumps({"version": 1, "skills": {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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
        return InitializationResult(yy_dir=yy, initialized=False)
    return InitializationResult(yy_dir=initialize_project(root), initialized=True)
