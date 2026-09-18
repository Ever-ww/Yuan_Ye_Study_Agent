"""Snapshot Manifest publication, validation, and materialization."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from .archive import _fsync_directory, _replace_with_retry
from .catalog import AgentHomeDurabilityCatalog
from .models import BackupManifest
from .object_store import BackupObjectStore, ObjectStoreContext


def canonical_manifest_payload(manifest: BackupManifest) -> bytes:
    value = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def finalize_manifest(manifest: BackupManifest) -> BackupManifest:
    digest = hashlib.sha256(canonical_manifest_payload(manifest)).hexdigest()
    return manifest.model_copy(update={"manifest_hash": digest})


def write_manifest(path: Path, manifest: BackupManifest) -> BackupManifest:
    finalized = finalize_manifest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.partial")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(finalized.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return finalized


def read_manifest(path: Path) -> BackupManifest:
    manifest = BackupManifest.model_validate_json(path.read_text(encoding="utf-8"), strict=True)
    if manifest.backup_format_version != 2:
        raise ValueError(f"Unsupported Snapshot Manifest version: {manifest.backup_format_version}")
    expected = hashlib.sha256(canonical_manifest_payload(manifest)).hexdigest()
    if manifest.manifest_hash != expected:
        raise ValueError("Snapshot Manifest hash mismatch")
    paths: set[str] = set()
    for member in manifest.files:
        AgentHomeDurabilityCatalog.validate_member_name(member.path)
        if member.path in paths:
            raise ValueError(f"Duplicate Snapshot Manifest member: {member.path}")
        if member.object_id is None or member.object_id != member.sha256:
            raise ValueError(f"Snapshot member has invalid object identity: {member.path}")
        paths.add(member.path)
    return manifest


def materialize_snapshot(
    manifest_path: Path,
    destination: Path,
    store: BackupObjectStore,
    context: ObjectStoreContext,
) -> BackupManifest:
    manifest = read_manifest(manifest_path)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for member in manifest.files:
            relative = AgentHomeDurabilityCatalog.validate_member_name(member.path)
            target = destination.joinpath(*relative.parts).resolve()
            if destination not in target.parents:
                raise ValueError(f"Snapshot member escapes destination: {member.path}")
            store.restore_object(member.object_id or "", target, context)
            if target.stat().st_size != member.size:
                raise ValueError(f"Snapshot member size mismatch: {member.path}")
        _fsync_directory(destination)
    except BaseException:
        import shutil

        shutil.rmtree(destination, ignore_errors=True)
        raise
    return manifest


def validate_sqlite_members(destination: Path, manifest: BackupManifest) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    for member in manifest.files:
        path = destination.joinpath(*Path(member.path).parts)
        try:
            with path.open("rb") as handle:
                is_sqlite = handle.read(16) == b"SQLite format 3\x00"
        except OSError as exc:
            errors.append(f"Cannot read {member.path}: {exc}")
            continue
        if not is_sqlite:
            continue
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("quick_check failed")
            foreign = db.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                raise RuntimeError(f"foreign_key_check found {len(foreign)} rows")
        except Exception as exc:
            errors.append(f"SQLite validation failed {member.path}: {exc}")
        finally:
            if db is not None:
                db.close()
    return not errors, tuple(errors)


__all__ = [
    "canonical_manifest_payload",
    "finalize_manifest",
    "materialize_snapshot",
    "read_manifest",
    "validate_sqlite_members",
    "write_manifest",
]
