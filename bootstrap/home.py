"""安装版 Agent Home 解析与旧源码状态的非破坏迁移。"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def platform_agent_home() -> Path:
    """返回 `.yy` 的父目录；默认就是当前用户主目录。"""
    explicit = os.getenv("YY_AGENT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path.home().resolve()


def legacy_platform_agent_home() -> Path:
    """返回 1.x 曾使用的平台数据容器，仅用于一次性迁移。"""
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return (base / "YuanYeAgent").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "YuanYeAgent").resolve()
    base = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return (base / "yuan-ye-agent").resolve()


def migrate_source_home(source_root: Path, target_root: Path) -> None:
    """把一个旧容器非破坏地并入统一 `.yy`，已存在文件永不覆盖。"""
    source, target = source_root.resolve(), target_root.resolve()
    if source == target:
        return
    marker = target / ".yy" / "agent-home-migration.json"
    existing = _read_marker(marker)
    migrated = {
        str(value)
        for value in existing.get("migrated_sources", [])
        if isinstance(value, str)
    }
    legacy_source = existing.get("source_root")
    if not migrated and isinstance(legacy_source, str):
        migrated.add(legacy_source)
    if str(source) in migrated:
        return
    source_yy = source / ".yy"
    target.mkdir(parents=True, exist_ok=True)
    if source_yy.is_dir():
        shutil.copytree(
            source_yy,
            target / ".yy",
            dirs_exist_ok=True,
            copy_function=_copy_missing,
            ignore=_migration_ignore(source_yy),
        )
        _partition_legacy_sessions(source, target / ".yy")
    source_skills = source / "skills"
    source_is_repository = (source / "run.py").is_file() and (source / "Agent").is_dir()
    if source_skills.is_dir() and not source_is_repository:
        shutil.copytree(
            source_skills,
            target / ".yy" / "skills" / "installed",
            dirs_exist_ok=True,
            copy_function=_copy_missing,
        )
    migrated.add(str(source))
    metadata = _read_marker(marker)
    code_source = metadata.get("source_root")
    if source_is_repository:
        code_source = str(source)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                **metadata,
                "version": 2,
                "source_root": code_source or str(source),
                "target_root": str(target),
                "migrated_sources": sorted(migrated),
                "last_migrated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source_preserved": True,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def legacy_gateway_active(root: Path) -> bool:
    """旧状态仍被 Gateway 持有时不复制活跃 SQLite，继续使用旧容器。"""
    lock_path = root.resolve() / ".yy" / "gateway" / "instance.lock"
    if not lock_path.exists():
        return False
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("r+b")
    except OSError:
        return True
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        except (OSError, BlockingIOError):
            return True
    finally:
        handle.close()


def _read_marker(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _copy_missing(source: str, destination: str) -> str:
    target = Path(destination)
    if not target.exists():
        shutil.copy2(source, target)
    return str(target)


def _migration_ignore(source_yy: Path):
    """Skip transient files and loose legacy Session facts during tree copy.

    Existing workspace partitions are still copied.  Loose Session records are
    routed exactly once by ``_partition_legacy_sessions`` so a previous target
    index cannot be mistaken for the current source's history.
    """
    legacy_session_root = (source_yy / "memory" / "session").resolve()
    transient = (
        "*.lock", "*.tmp", "*.sqlite3-wal", "*.sqlite3-shm",
        "instance.json", "stop.request",
    )

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {
            name for name in names
            if any(fnmatch.fnmatch(name, pattern) for pattern in transient)
        }
        if Path(directory).resolve() == legacy_session_root:
            ignored.update(
                name for name in names
                if name == "index.json" or name.endswith(".jsonl")
            )
        return ignored

    return ignore


def _partition_legacy_sessions(source_root: Path, target_yy: Path) -> None:
    source_session_root = source_root / ".yy" / "memory" / "session"
    index = source_session_root / "index.json"
    if not index.is_file():
        return
    key = hashlib.sha256(
        os.path.normcase(str(source_root.resolve())).encode("utf-8"),
    ).hexdigest()[:16]
    destination = target_yy / "memory" / "session" / key
    destination.mkdir(parents=True, exist_ok=True)
    try:
        value = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"旧 Session 索引无法迁移：{index}") from exc
    source_sessions = value.get("sessions", {})
    if not isinstance(source_sessions, dict):
        raise RuntimeError(f"旧 Session 索引结构无效：{index}")
    destination_index = destination / "index.json"
    if destination_index.is_file():
        try:
            destination_value = json.loads(destination_index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"目标 Session 索引无法合并：{destination_index}") from exc
    else:
        destination_value = {"version": 1, "sessions": {}}
    target_sessions = destination_value.get("sessions")
    if not isinstance(target_sessions, dict):
        raise RuntimeError(f"目标 Session 索引结构无效：{destination_index}")
    for session_id, metadata in source_sessions.items():
        if not isinstance(metadata, dict):
            raise RuntimeError(f"旧 Session 元数据无效：{session_id}")
        filenames = metadata.get("files", [])
        if not isinstance(filenames, list) or not filenames:
            raise RuntimeError(f"旧 Session 文件索引无效：{session_id}")
        for filename in filenames:
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise RuntimeError(f"旧 Session 文件名越界：{filename}")
            source = source_session_root / filename
            if not source.is_file():
                raise RuntimeError(f"旧 Session 文件缺失：{source}")
            target = destination / filename
            if target.is_file():
                if _file_hash(source) != _file_hash(target):
                    raise RuntimeError(f"Session 迁移内容冲突：{target}")
            else:
                shutil.copy2(source, target)
        existing = target_sessions.get(session_id)
        if existing is not None and not _session_metadata_compatible(existing, metadata):
            raise RuntimeError(f"Session 迁移索引冲突：{session_id}")
        if existing is None:
            target_sessions[session_id] = metadata
    _write_json_atomic(destination_index, destination_value)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _session_metadata_compatible(left: object, right: object) -> bool:
    """Compare immutable legacy identity while tolerating additive v1 fields."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for name in ("created_at", "latest_file", "files"):
        if left.get(name) != right.get(name):
            return False
    left_catalog = left.get("skill_catalog")
    right_catalog = right.get("skill_catalog")
    return left_catalog is None or right_catalog is None or left_catalog == right_catalog


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
