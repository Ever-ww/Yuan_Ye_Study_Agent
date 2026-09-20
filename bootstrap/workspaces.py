"""Workspace-owned state, global discovery manifest, and one-time migration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


WORKSPACE_SCHEMA_VERSION = 1
WORKSPACE_MIGRATION_VERSION = 2


def workspace_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:16]


def workspace_state_dir(path: Path) -> Path:
    return path.resolve() / ".yy"


def ensure_workspace_initialized(
    workspace_root: Path,
    *,
    agent_root: Path,
    name: str | None = None,
    touch_recent: bool = False,
    last_opened_at: str | None = None,
) -> dict[str, Any]:
    """Create one isolated Workspace state tree and register it globally."""

    workspace = workspace_root.resolve()
    if not workspace.is_dir():
        raise ValueError(f"Workspace is not a directory: {workspace}")
    state = workspace_state_dir(workspace)
    if state.exists() and state.is_symlink():
        raise PermissionError(f"Workspace state directory cannot be a symlink: {state}")
    state.mkdir(parents=True, exist_ok=True)
    identifier = workspace_id(workspace)
    selected_name = (name or workspace.name or str(workspace)).strip()
    _migrate_legacy_workspace_state(
        agent_root.resolve() / ".yy", state, identifier,
    )

    # Import lazily: bootstrap initialization is also imported by RuntimeConfig.
    from memory import MemoryStore
    from paper_library import PaperIndex
    from reference import PaperFile, ReferenceStore
    from skill import SkillService

    MemoryStore(
        state / "memory",
        workspace_root=workspace,
        agent_root=agent_root,
        partition_by_workspace=False,
    )
    reference_store = ReferenceStore(state / "reference" / "reference.sqlite3")
    papers = state / "papers"
    papers.mkdir(parents=True, exist_ok=True)
    paper_index = papers / "index.json"
    if not paper_index.exists():
        _atomic_json(paper_index, PaperIndex().model_dump(mode="json"))
    _reconcile_legacy_paper_files(
        workspace, papers, paper_index, reference_store, PaperIndex, PaperFile,
    )
    bundled_skills = _bundled_skills_root()
    if bundled_skills.is_dir():
        SkillService(agent_root, workspace, bundled_skills.parent)

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    metadata_path = state / "workspace.json"
    previous = _read_json(metadata_path)
    metadata = {
        "workspace_id": identifier,
        "name": selected_name,
        "version": WORKSPACE_SCHEMA_VERSION,
        "migration_version": WORKSPACE_MIGRATION_VERSION,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
    }
    _atomic_json(metadata_path, metadata)
    _update_global_manifest(
        agent_root.resolve(), workspace, metadata,
        touch_recent=touch_recent, last_opened_at=last_opened_at,
    )
    return metadata


def workspace_manifest(agent_root: Path) -> tuple[dict[str, Any], ...]:
    value = _read_json(agent_root.resolve() / ".yy" / "workspaces.json")
    items = value.get("workspaces", [])
    return tuple(item for item in items if isinstance(item, dict)) if isinstance(items, list) else ()


def default_workspace_root(agent_root: Path, fallback: Path) -> Path:
    """Use the most recently opened registered Workspace, never the launch cwd by accident."""

    for item in workspace_manifest(agent_root):
        value = item.get("path")
        if isinstance(value, str) and value:
            candidate = Path(value).resolve()
            if candidate.is_dir():
                return candidate
    return fallback.resolve()


def unregister_workspace(agent_root: Path, identifier: str) -> None:
    """Forget global discovery metadata without deleting Workspace-owned data."""

    target = agent_root.resolve() / ".yy" / "workspaces.json"
    current = _read_json(target)
    items = current.get("workspaces", [])
    if not isinstance(items, list):
        return
    _atomic_json(target, {
        "version": 1,
        "workspaces": [
            item for item in items
            if isinstance(item, dict) and item.get("workspace_id") != identifier
        ],
    })


def _update_global_manifest(
    agent_root: Path,
    workspace: Path,
    metadata: dict[str, Any],
    *,
    touch_recent: bool,
    last_opened_at: str | None,
) -> None:
    target = agent_root / ".yy" / "workspaces.json"
    current = _read_json(target)
    existing = {
        str(item.get("workspace_id")): item
        for item in current.get("workspaces", [])
        if isinstance(item, dict) and item.get("workspace_id")
    }
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    identifier = str(metadata["workspace_id"])
    prior = existing.get(identifier, {})
    existing[identifier] = {
        "workspace_id": identifier,
        "name": metadata["name"],
        "path": str(workspace),
        "version": int(metadata["version"]),
        "migration_version": int(metadata["migration_version"]),
        "created_at": prior.get("created_at") or metadata["created_at"],
        "last_opened_at": (
            now if touch_recent else prior.get("last_opened_at") or last_opened_at or now
        ),
    }
    _atomic_json(target, {
        "version": 1,
        "workspaces": sorted(
            existing.values(), key=lambda item: str(item["last_opened_at"]), reverse=True,
        ),
    })


def _migrate_legacy_workspace_state(
    global_home: Path,
    destination: Path,
    identifier: str,
) -> None:
    """Move legacy global data once; route partitioned Sessions by Workspace."""

    if global_home.resolve() == destination.resolve():
        return

    marker_path = global_home / "workspace-data-migration.json"
    marker = _read_json(marker_path)
    migrated = {
        str(item) for item in marker.get("workspace_ids", []) if isinstance(item, str)
    }
    completed_version = int(marker.get("version") or 0)
    if identifier in migrated and completed_version >= WORKSPACE_MIGRATION_VERSION:
        return

    legacy_memory = global_home / "memory"
    shared_owner = marker.get("shared_owner_workspace_id")
    if not isinstance(shared_owner, str) or not shared_owner:
        shared_owner = identifier
        marker["shared_owner_workspace_id"] = identifier

    cleanup: list[Path] = []
    if shared_owner == identifier:
        legacy_sessions = legacy_memory / "session"
        _migrate_legacy_sessions(legacy_sessions, destination / "memory" / "session", identifier)
        if legacy_sessions.is_dir():
            cleanup.append(legacy_sessions)
        for name in ("profile",):
            source = legacy_memory / name
            if source.is_dir():
                target = destination / "memory" / name
                if source.resolve() != target.resolve():
                    _merge_tree(source, target)
                    cleanup.append(source)
        for name in ("memory.sqlite3", "index.sqlite3"):
            source = legacy_memory / name
            if source.is_file():
                target = destination / "memory" / name
                if source.resolve() == target.resolve():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and _sha256(target) != _sha256(source):
                    raise RuntimeError(f"Workspace migration conflict: {target}")
                if not target.exists():
                    shutil.copy2(source, target)
                cleanup.append(source)
        if legacy_memory.is_dir():
            known = {"session", "profile", "memory.sqlite3", "index.sqlite3"}
            for source in sorted(legacy_memory.iterdir()):
                if source.name in known:
                    continue
                target = destination / "memory" / source.name
                if source.is_dir():
                    _merge_tree(source, target)
                elif source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and _sha256(target) != _sha256(source):
                        raise RuntimeError(f"Workspace migration conflict: {target}")
                    if not target.exists():
                        shutil.copy2(source, target)
                cleanup.append(source)
        for name in (
            "reference", "papers", "skills", "dream", "harness-evolution", "sandbox",
        ):
            source = global_home / name
            if source.is_dir():
                target = destination / name
                if source.resolve() != target.resolve():
                    _merge_tree(source, target)
                    cleanup.append(source)
        legacy_projects = global_home / "projects"
        if legacy_projects.is_dir():
            _merge_tree(legacy_projects, destination / "legacy" / "projects")
            cleanup.append(legacy_projects)
    else:
        legacy_partition = legacy_memory / "session" / identifier
        if legacy_partition.is_dir():
            _migrate_legacy_sessions(
                legacy_partition, destination / "memory" / "session", identifier,
            )
            cleanup.append(legacy_partition)

    # Every source remains intact until all copies and hash comparisons above pass.
    for source in cleanup:
        if source.is_dir():
            _remove_tree(source)
        elif source.exists():
            source.unlink()
    if legacy_memory.is_dir() and not any(legacy_memory.iterdir()):
        legacy_memory.rmdir()

    migrated.add(identifier)
    marker.update({
        "version": WORKSPACE_MIGRATION_VERSION,
        "workspace_ids": sorted(migrated),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    _atomic_json(marker_path, marker)


def _migrate_legacy_sessions(source: Path, destination: Path, identifier: str) -> None:
    """Flatten old Workspace partitions and merge their canonical Session indexes."""

    if not source.is_dir():
        return
    if source.resolve() == destination.resolve():
        return
    destination.mkdir(parents=True, exist_ok=True)
    index = _read_json(destination / "index.json") or {"version": 1, "sessions": {}}
    sessions = index.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    roots = [source, *(item for item in sorted(source.iterdir()) if item.is_dir())]
    for root in roots:
        source_index = _read_json(root / "index.json").get("sessions", {})
        if isinstance(source_index, dict):
            for session_id, value in source_index.items():
                if isinstance(value, dict):
                    value = {**value, "workspace_id": identifier}
                existing = sessions.get(session_id)
                if existing is not None and existing != value:
                    raise RuntimeError(f"Workspace Session migration conflict: {session_id}")
                sessions[session_id] = value
        for item in sorted(root.iterdir()):
            if item.name == "index.json" or item.is_dir():
                continue
            target = destination / item.name
            if target.exists() and _sha256(target) != _sha256(item):
                raise RuntimeError(f"Workspace migration conflict: {target}")
            if not target.exists():
                shutil.copy2(item, target)
    _atomic_json(destination / "index.json", {"version": 1, "sessions": sessions})


def _reconcile_legacy_paper_files(
    workspace: Path,
    papers_root: Path,
    index_path: Path,
    reference_store: Any,
    paper_index_type: Any,
    paper_file_type: Any,
) -> None:
    """Link legacy Paper Library files to their migrated Reference records."""

    try:
        index = paper_index_type.model_validate_json(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    root = papers_root.resolve()
    identifier = workspace_id(workspace)
    for record in index.papers.values():
        if not record.reference_paper_id or not record.pdf_path or not record.sha256:
            continue
        try:
            paper = reference_store.get_paper(record.reference_paper_id)
        except KeyError:
            continue
        if any(item.sha256 == record.sha256 for item in paper.files):
            continue
        path = (papers_root / Path(record.pdf_path)).resolve()
        if root not in path.parents or path.is_symlink() or not path.is_file():
            continue
        if _sha256(path) != record.sha256:
            continue
        reference_store.add_file(record.reference_paper_id, paper_file_type(
            workspace_hash=identifier,
            workspace_root=str(workspace),
            relative_path=path.relative_to(workspace).as_posix(),
            absolute_path=str(path),
            sha256=record.sha256,
            mime_type=record.content_type or "application/pdf",
            size_bytes=path.stat().st_size,
            is_primary=True,
        ))


def _merge_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_symlink():
            raise PermissionError(f"Workspace migration refuses symlink: {item}")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _sha256(target) != _sha256(item):
                raise RuntimeError(f"Workspace migration conflict: {target}")
        else:
            shutil.copy2(item, target)


def _remove_tree(path: Path) -> None:
    def writable_retry(function, value, _error) -> None:
        os.chmod(value, stat.S_IWRITE)
        function(value)

    shutil.rmtree(path, onerror=writable_retry)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _bundled_skills_root() -> Path:
    candidates = (
        Path(__file__).resolve().parent.parent / "skills",
        Path(sys.prefix).resolve() / "skills",
    )
    return next((path for path in candidates if path.is_dir()), candidates[0])


__all__ = [
    "WORKSPACE_MIGRATION_VERSION",
    "WORKSPACE_SCHEMA_VERSION",
    "ensure_workspace_initialized",
    "default_workspace_root",
    "workspace_id",
    "workspace_manifest",
    "workspace_state_dir",
    "unregister_workspace",
]
