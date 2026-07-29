"""安装版 Agent Home 解析与旧源码状态的非破坏迁移。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def platform_agent_home() -> Path:
    """返回跨平台、当前用户可写的默认 Agent Home。"""
    explicit = os.getenv("YY_AGENT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return (base / "YuanYeAgent").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "YuanYeAgent").resolve()
    base = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return (base / "yuan-ye-agent").resolve()


def migrate_source_home(source_root: Path, target_root: Path) -> None:
    """首次安装只复制旧 `.yy`/skills，并为旧主 Session 建立 workspace 分区。"""
    source, target = source_root.resolve(), target_root.resolve()
    if source == target:
        return
    marker = target / ".yy" / "agent-home-migration.json"
    if marker.exists():
        return
    source_yy = source / ".yy"
    target.mkdir(parents=True, exist_ok=True)
    if source_yy.is_dir():
        shutil.copytree(source_yy, target / ".yy", dirs_exist_ok=True)
        _partition_legacy_sessions(source, target / ".yy")
    source_skills = source / "skills"
    if source_skills.is_dir():
        shutil.copytree(source_skills, target / "skills", dirs_exist_ok=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "version": 1,
                "source_root": str(source),
                "target_root": str(target),
                "copied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source_preserved": True,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def _partition_legacy_sessions(source_root: Path, target_yy: Path) -> None:
    session_root = target_yy / "memory" / "session"
    index = session_root / "index.json"
    if not index.is_file():
        return
    key = hashlib.sha256(
        os.path.normcase(str(source_root.resolve())).encode("utf-8"),
    ).hexdigest()[:16]
    destination = session_root / key
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(index, destination / "index.json")
    try:
        value = json.loads(index.read_text(encoding="utf-8"))
        filenames = {
            filename
            for session in value.get("sessions", {}).values()
            if isinstance(session, dict)
            for filename in session.get("files", [])
            if isinstance(filename, str)
        }
    except (OSError, json.JSONDecodeError):
        filenames = set()
    for filename in filenames:
        source = session_root / filename
        if source.is_file():
            shutil.copy2(source, destination / filename)
