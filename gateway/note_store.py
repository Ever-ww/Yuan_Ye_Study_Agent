"""Workspace-local Markdown notes and their lightweight tree index."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


NoteKind = Literal["note", "folder"]
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")


class NoteConflict(RuntimeError):
    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("Note was modified by another client")
        self.current = current


class NoteStore:
    """Persist note metadata in SQLite and Markdown bodies as user-readable files."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.notes_root = self.workspace_root / "notes"
        self.control_root = self.workspace_root / ".yy" / "notes"
        self.database_path = self.control_root / "notes.sqlite3"
        self._index_lock = threading.RLock()
        self._last_disk_signature: tuple[tuple[str, int, int, bool], ...] | None = None
        self._watcher: Any | None = None
        self._native_watcher_thread: threading.Thread | None = None
        self._native_watcher_handle: Any | None = None
        self._native_watcher_stop = threading.Event()
        self._watcher_timer: threading.Timer | None = None
        self._watcher_lock = threading.RLock()
        self._watcher_callback: Callable[[tuple[str, ...]], None] | None = None
        self._watcher_paths: set[str] = set()
        self.notes_root.mkdir(parents=True, exist_ok=True)
        self.control_root.mkdir(parents=True, exist_ok=True)
        self._validate_managed_path(self.notes_root)
        self._validate_managed_path(self.control_root)
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS note_nodes(
              note_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL CHECK(kind IN ('note','folder')),
              name TEXT NOT NULL,
              parent_id TEXT REFERENCES note_nodes(note_id) ON DELETE CASCADE,
              file_path TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_note_nodes_parent_updated
              ON note_nodes(parent_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS note_trash(
              trash_id TEXT PRIMARY KEY,
              original_note_id TEXT NOT NULL,
              name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('note','folder')),
              original_file_path TEXT NOT NULL,
              trashed_path TEXT NOT NULL,
              deleted_at TEXT NOT NULL
            );
            """)
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(note_nodes)")}
            if "file_path" not in columns:
                connection.execute("ALTER TABLE note_nodes ADD COLUMN file_path TEXT")
            for row in connection.execute("SELECT note_id,kind,name FROM note_nodes WHERE file_path IS NULL").fetchall():
                suffix = ".md" if row["kind"] == "note" else ""
                candidate = self.notes_root / f"{row['note_id']}{suffix}"
                connection.execute("UPDATE note_nodes SET file_path=? WHERE note_id=?", (candidate.relative_to(self.notes_root).as_posix(), row["note_id"]))
        # Markdown is the source of truth. If the rebuildable index is empty,
        # recover notes from files before serving the first request.
        with self._connect() as connection:
            has_rows = connection.execute("SELECT 1 FROM note_nodes LIMIT 1").fetchone()
        if has_rows is None:
            self.reindex()

    def start_watcher(self, callback: Callable[[tuple[str, ...]], None] | None = None) -> bool:
        """Watch the Markdown vault with the platform's native backend.

        watchdog selects ReadDirectoryChangesW on Windows, inotify on Linux and
        FSEvents/kqueue on macOS.  The watcher only invalidates/rebuilds the
        small SQLite projection; Markdown files remain the source of truth.
        """
        with self._watcher_lock:
            self._watcher_callback = callback
            if self._watcher is not None:
                return True
            try:
                from watchdog.events import FileSystemEventHandler
                from watchdog.observers import Observer
            except ImportError:
                # Windows has a stdlib-only fallback so a partially provisioned
                # development environment still gets ReadDirectoryChangesW.
                if os.name != "nt":
                    return False
                return self._start_windows_watcher_locked()

            store = self

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):  # type: ignore[no-untyped-def]
                    if getattr(event, "is_directory", False):
                        return
                    paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
                    for selected in paths:
                        if selected:
                            store._queue_watcher_path(Path(selected))

            observer = Observer()
            observer.schedule(Handler(), str(self.notes_root), recursive=True)
            observer.daemon = True
            observer.start()
            self._watcher = observer
            return True

    def stop_watcher(self) -> None:
        with self._watcher_lock:
            if self._watcher_timer is not None:
                self._watcher_timer.cancel()
                self._watcher_timer = None
            observer, self._watcher = self._watcher, None
            self._watcher_callback = None
            self._watcher_paths.clear()
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)
        self._native_watcher_stop.set()
        handle, self._native_watcher_handle = self._native_watcher_handle, None
        if handle is not None:
            try:
                import ctypes
                if hasattr(ctypes.windll.kernel32, "CancelIoEx"):
                    ctypes.windll.kernel32.CancelIoEx(handle, None)
                ctypes.windll.kernel32.CloseHandle(handle)
            except (AttributeError, OSError):
                pass
        thread, self._native_watcher_thread = self._native_watcher_thread, None
        if thread is not None:
            thread.join(timeout=2)

    def _start_windows_watcher_locked(self) -> bool:
        import ctypes
        from ctypes import wintypes

        FILE_LIST_DIRECTORY = 0x0001
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_NOTIFY_CHANGE = 0x00000001 | 0x00000002 | 0x00000004 | 0x00000010 | 0x00000020
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID, wintypes.LPVOID]
        kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if hasattr(kernel32, "CancelIoEx"):
            kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
            kernel32.CancelIoEx.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(self.notes_root), FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            return False
        self._native_watcher_stop.clear()
        self._native_watcher_handle = handle

        def listen() -> None:
            buffer = ctypes.create_string_buffer(64 * 1024)
            bytes_returned = wintypes.DWORD()
            while not self._native_watcher_stop.is_set():
                ok = kernel32.ReadDirectoryChangesW(
                    handle, ctypes.byref(buffer), len(buffer), True,
                    FILE_NOTIFY_CHANGE, ctypes.byref(bytes_returned), None, None,
                )
                if not ok or not bytes_returned.value:
                    if self._native_watcher_stop.is_set():
                        break
                    continue
                offset = 0
                raw = buffer.raw
                while offset + 12 <= bytes_returned.value:
                    next_offset = int.from_bytes(raw[offset:offset + 4], "little")
                    name_length = int.from_bytes(raw[offset + 8:offset + 12], "little")
                    name_start = offset + 12
                    name = raw[name_start:name_start + name_length].decode("utf-16-le", errors="ignore")
                    if name:
                        self._queue_watcher_path(self.notes_root / name)
                    if not next_offset:
                        break
                    offset += next_offset

        thread = threading.Thread(target=listen, name="yy-note-native-watcher", daemon=True)
        self._native_watcher_thread = thread
        thread.start()
        return True

    def _queue_watcher_path(self, path: Path) -> None:
        try:
            relative = path.resolve().relative_to(self.notes_root.resolve()).as_posix()
        except (OSError, ValueError):
            return
        if relative.startswith(".yy/") or not (relative.casefold().endswith(".md") or not path.suffix):
            return
        with self._watcher_lock:
            self._watcher_paths.add(relative)
            if self._watcher_timer is None:
                self._watcher_timer = threading.Timer(0.18, self._flush_watcher)
                self._watcher_timer.daemon = True
                self._watcher_timer.start()

    def _flush_watcher(self) -> None:
        with self._watcher_lock:
            paths = tuple(sorted(self._watcher_paths))
            self._watcher_paths.clear()
            self._watcher_timer = None
        if not paths:
            return
        signature = self._disk_signature()
        if signature == self._last_disk_signature:
            return
        try:
            self.reindex()
        except (OSError, sqlite3.Error):
            return
        callback = self._watcher_callback
        if callback is not None:
            try:
                callback(paths)
            except Exception:
                # A notification failure must never stop the native watcher.
                return

    def list(self, query: str = "") -> list[dict[str, Any]]:
        self._sync_index_from_disk()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM note_nodes ORDER BY updated_at DESC, name COLLATE NOCASE",
            ).fetchall()
        needle = query.strip().casefold()
        if needle:
            by_id = {str(row["note_id"]): row for row in rows}
            included: set[str] = set()
            for row in rows:
                content, metadata = self._read_document(str(row["note_id"])) if row["kind"] == "note" else ("", {})
                searchable = " ".join((str(row["name"]), content, " ".join(metadata.get("tags", [])), self._logical_path_from_rows(row, by_id)))
                if needle in searchable.casefold():
                    current: sqlite3.Row | None = row
                    while current is not None:
                        current_id = str(current["note_id"])
                        if current_id in included:
                            break
                        included.add(current_id)
                        parent_id = current["parent_id"]
                        current = by_id.get(str(parent_id)) if parent_id else None
            rows = [row for row in rows if str(row["note_id"]) in included]
        return [self._public(row, include_content=False) for row in rows]

    def get(self, note_id: str) -> dict[str, Any]:
        self._sync_index_from_disk()
        with self._connect() as connection:
            row = self._require(connection, note_id)
        return self._public(row, include_content=True)

    def create(self, *, name: str = "未命名笔记", kind: NoteKind = "note", parent_id: str | None = None, content: str = "") -> dict[str, Any]:
        clean_name = self._clean_name(name, default="未命名笔记" if kind == "note" else "新文件夹")
        body, imported_metadata = _parse_frontmatter(content) if kind == "note" else ("", {})
        with self._connect() as connection:
            self._validate_parent(connection, parent_id)
            note_id, timestamp = uuid4().hex, _now()
            parent_path = self._parent_directory(connection, parent_id)
            target = self._unique_path(parent_path, clean_name, kind)
            relative_path = target.relative_to(self.notes_root).as_posix()
            connection.execute(
                "INSERT INTO note_nodes(note_id,kind,name,parent_id,file_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (note_id, kind, clean_name, parent_id, relative_path, timestamp, timestamp),
            )
        if kind == "folder":
            target.mkdir(parents=True, exist_ok=False)
        else:
            self._write_body(note_id, body, metadata={
                "id": note_id, "title": clean_name, "created": timestamp,
                "updated": timestamp, "parent_id": parent_id,
                "tags": _clean_tags(imported_metadata.get("tags", [])),
                "aliases": imported_metadata.get("aliases", []),
            })
        self._remember_disk_signature()
        return self.get(note_id)

    def update(
        self, note_id: str, *, name: str | None = None, content: str | None = None,
        tags: list[str] | None = None, expected_etag: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._require(connection, note_id)
        current = self._public(row, include_content=True)
        if current["kind"] == "folder" and content is not None:
            raise ValueError("Folders do not have Markdown content")
        if expected_etag is not None and expected_etag != current["etag"]:
            raise NoteConflict(current)
        clean_name = self._clean_name(name, default=current["name"]) if name is not None else current["name"]
        timestamp = _now()
        if name is not None and clean_name != current["name"]:
            self._rename_path(row, clean_name)
        with self._connect() as connection:
            connection.execute(
                "UPDATE note_nodes SET name=?,updated_at=? WHERE note_id=?",
                (clean_name, timestamp, note_id),
            )
        if current["kind"] == "note" and (content is not None or name is not None or tags is not None):
            existing_content, existing_metadata = self._read_document(note_id)
            aliases = list(existing_metadata.get("aliases", []))
            if name is not None and clean_name != current["name"] and current["name"] not in aliases:
                aliases.append(current["name"])
            self._write_body(note_id, existing_content if content is None else content, metadata={
                **existing_metadata,
                "id": note_id,
                "title": clean_name,
                "created": existing_metadata.get("created", row["created_at"]),
                "updated": timestamp,
                "parent_id": row["parent_id"],
                "tags": existing_metadata.get("tags", []) if tags is None else _clean_tags(tags),
                "aliases": aliases,
            })
        self._remember_disk_signature()
        return self.get(note_id)

    def move(self, note_id: str, parent_id: str | None) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._require(connection, note_id)
            self._validate_parent(connection, parent_id)
            if parent_id == note_id or self._is_descendant(connection, parent_id, note_id):
                raise ValueError("A note cannot be moved into itself or its descendants")
            source = self._node_path(row)
            target = self._unique_path(self._parent_directory(connection, parent_id), str(row["name"]), str(row["kind"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            self._replace_path_prefix(connection, str(row["file_path"]), target.relative_to(self.notes_root).as_posix())
            connection.execute(
                "UPDATE note_nodes SET parent_id=?,updated_at=? WHERE note_id=?",
                (parent_id, _now(), note_id),
            )
        if row["kind"] == "note":
            content, metadata = self._read_document(note_id)
            self._write_body(note_id, content, metadata={**metadata, "parent_id": parent_id, "updated": _now()})
        self._remember_disk_signature()
        return self.get(note_id)

    def delete(self, note_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._require(connection, note_id)
        path = self._node_path(row)
        trash_id = uuid4().hex
        trash_directory = self.control_root / "trash" / trash_id
        trash_directory.mkdir(parents=True, exist_ok=False)
        target = trash_directory / path.name
        os.replace(path, target)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO note_trash(trash_id,original_note_id,name,kind,original_file_path,trashed_path,deleted_at) VALUES(?,?,?,?,?,?,?)",
                    (trash_id, note_id, row["name"], row["kind"], row["file_path"], target.relative_to(self.control_root).as_posix(), _now()),
                )
                connection.execute("DELETE FROM note_nodes WHERE note_id=?", (note_id,))
        except Exception:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, path)
            shutil.rmtree(trash_directory, ignore_errors=True)
            raise
        self._remember_disk_signature()
        return {"deleted": True, "trash_id": trash_id}

    def trash(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trash_id,original_note_id,name,kind,original_file_path,deleted_at FROM note_trash ORDER BY deleted_at DESC",
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_trash(self, trash_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM note_trash WHERE trash_id=?", (trash_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown trashed note: {trash_id}")
        source = self.control_root / str(row["trashed_path"])
        self._validate_managed_path(source)
        original = self.notes_root / str(row["original_file_path"])
        original.parent.mkdir(parents=True, exist_ok=True)
        target = original
        if target.exists():
            target = self._unique_path(target.parent, str(row["name"]), str(row["kind"]))
        os.replace(source, target)
        shutil.rmtree(source.parent, ignore_errors=True)
        with self._connect() as connection:
            connection.execute("DELETE FROM note_trash WHERE trash_id=?", (trash_id,))
        self.reindex()
        relative = target.relative_to(self.notes_root).as_posix()
        with self._connect() as connection:
            restored = connection.execute("SELECT * FROM note_nodes WHERE file_path=?", (relative,)).fetchone()
        if restored is None:
            raise RuntimeError("Restored note could not be reindexed")
        return self._public(restored, include_content=restored["kind"] == "note")

    def revisions(self, note_id: str) -> list[dict[str, str]]:
        self.get(note_id)
        directory = self.control_root / "revisions" / note_id
        if not directory.is_dir():
            return []
        return [
            {"revision_id": path.stem, "created_at": path.stem.split("-", 1)[0]}
            for path in sorted(directory.glob("*.md"), reverse=True)
        ]

    def restore_revision(self, note_id: str, revision_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9T.]+Z-[0-9a-f]{12}", revision_id):
            raise ValueError("Invalid note revision")
        revision = self.control_root / "revisions" / note_id / f"{revision_id}.md"
        if not revision.is_file():
            raise KeyError(f"Unknown note revision: {revision_id}")
        content, metadata = _parse_frontmatter(revision.read_text(encoding="utf-8"))
        current = self.get(note_id)
        self._write_body(note_id, content, metadata={
            **metadata, "id": note_id, "title": current["name"], "updated": _now(),
        })
        with self._connect() as connection:
            connection.execute("UPDATE note_nodes SET updated_at=? WHERE note_id=?", (_now(), note_id))
        self._remember_disk_signature()
        return self.get(note_id)

    def save_asset(self, note_id: str, filename: str, data: bytes, *, kind: str) -> dict[str, str]:
        note = self.get(note_id)
        if note["kind"] != "note":
            raise ValueError("Assets can only be attached to notes")
        if kind not in {"image", "attachment"}:
            raise ValueError("Asset kind must be image or attachment")
        directory = self.workspace_root / "assets" / ("images" if kind == "image" else "attachments")
        directory.mkdir(parents=True, exist_ok=True)
        self._validate_managed_path(directory)
        clean_name = _safe_file_name(Path(filename).name)
        target = directory / clean_name
        counter = 2
        while target.exists():
            target = directory / f"{Path(clean_name).stem} ({counter}){Path(clean_name).suffix}"
            counter += 1
        descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=directory)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        note_directory = self._body_path(note_id).parent
        markdown_path = os.path.relpath(target, note_directory).replace("\\", "/")
        return {
            "name": target.name,
            "path": target.relative_to(self.workspace_root).as_posix(),
            "markdown_path": markdown_path,
        }

    def asset_path(self, relative_path: str) -> Path:
        assets_root = (self.workspace_root / "assets").resolve()
        candidate = self.workspace_root / relative_path
        self._validate_managed_path(candidate)
        path = candidate.resolve()
        if assets_root not in path.parents or not path.is_file():
            raise FileNotFoundError(relative_path)
        return path

    def raw_path(self, note_id: str) -> Path:
        note = self.get(note_id)
        if note["kind"] != "note":
            raise ValueError("Folders do not have Markdown content")
        return self._body_path(note_id)

    def _public(self, row: sqlite3.Row, *, include_content: bool) -> dict[str, Any]:
        note_id = str(row["note_id"])
        raw_content, metadata = self._read_document(note_id) if row["kind"] == "note" else ("", {})
        content = raw_content if include_content else ""
        relations = self._relations(note_id, raw_content) if include_content and row["kind"] == "note" else {"links": [], "backlinks": []}
        return {
            "note_id": note_id,
            "kind": str(row["kind"]),
            "name": str(row["name"]),
            "parent_id": row["parent_id"],
            "path": self._logical_path(note_id),
            "content": content,
            "etag": self._file_etag(note_id) if row["kind"] == "note" else _etag(""),
            "tags": metadata.get("tags", []),
            "file_path": f"notes/{str(row['file_path']).replace(chr(92), '/')}",
            **relations,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _logical_path(self, note_id: str) -> str:
        parts: list[str] = []
        with self._connect() as connection:
            current = self._require(connection, note_id)
            while current is not None:
                parts.append(str(current["name"]))
                parent_id = current["parent_id"]
                current = self._require(connection, str(parent_id)) if parent_id else None
        return " / ".join(reversed(parts))

    @staticmethod
    def _logical_path_from_rows(row: sqlite3.Row, rows: dict[str, sqlite3.Row]) -> str:
        parts: list[str] = []
        current: sqlite3.Row | None = row
        while current is not None:
            parts.append(str(current["name"]))
            parent_id = current["parent_id"]
            current = rows.get(str(parent_id)) if parent_id else None
        return " / ".join(reversed(parts))

    def _node_path(self, row: sqlite3.Row) -> Path:
        relative = Path(str(row["file_path"] or ""))
        candidate = self.notes_root / relative
        self._validate_managed_path(candidate, allow_missing=True)
        path = candidate.resolve()
        if path != self.notes_root.resolve() and self.notes_root.resolve() not in path.parents:
            raise PermissionError("Note path is outside the Workspace")
        return path

    def _validate_managed_path(self, path: Path, *, allow_missing: bool = False) -> None:
        if ".." in path.parts:
            raise PermissionError("Note path cannot contain parent traversal")
        resolved = path.resolve(strict=False)
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise PermissionError("Note path is outside the Workspace")
        relative = path.absolute().relative_to(self.workspace_root)
        current = self.workspace_root
        for part in relative.parts:
            current /= part
            if not current.exists():
                if allow_missing:
                    return
                raise FileNotFoundError(current)
            info = current.lstat()
            if current.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                raise PermissionError("Note path cannot contain a link or reparse point")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise PermissionError("Note files cannot be hard linked")

    def _parent_directory(self, connection: sqlite3.Connection, parent_id: str | None) -> Path:
        if parent_id is None:
            return self.notes_root
        parent = self._require(connection, parent_id)
        if parent["kind"] != "folder":
            raise ValueError("Note parent must be a folder")
        return self._node_path(parent)

    def _unique_path(self, parent: Path, name: str, kind: str) -> Path:
        clean = _safe_file_name(name)
        suffix = ".md" if kind == "note" else ""
        candidate = parent / f"{clean}{suffix}"
        counter = 2
        while candidate.exists():
            candidate = parent / f"{clean} ({counter}){suffix}"
            counter += 1
        return candidate

    def _rename_path(self, row: sqlite3.Row, clean_name: str) -> None:
        source = self._node_path(row)
        suffix = ".md" if row["kind"] == "note" else ""
        target = source.with_name(f"{_safe_file_name(clean_name)}{suffix}")
        if target != source and target.exists():
            target = self._unique_path(source.parent, clean_name, str(row["kind"]))
        if target != source:
            os.replace(source, target)
            with self._connect() as connection:
                self._replace_path_prefix(connection, str(row["file_path"]), target.relative_to(self.notes_root).as_posix())

    @staticmethod
    def _replace_path_prefix(connection: sqlite3.Connection, old: str, new: str) -> None:
        rows = connection.execute("SELECT note_id,file_path FROM note_nodes").fetchall()
        for row in rows:
            path = str(row["file_path"] or "")
            if path == old or path.startswith(old.rstrip("/") + "/"):
                updated = new + path[len(old):]
                connection.execute("UPDATE note_nodes SET file_path=? WHERE note_id=?", (updated, row["note_id"]))

    def _body_path(self, note_id: str) -> Path:
        with self._connect() as connection:
            row = self._require(connection, note_id)
        return self._node_path(row)

    def _file_etag(self, note_id: str) -> str:
        try:
            return hashlib.sha256(self._body_path(note_id).read_bytes()).hexdigest()
        except FileNotFoundError:
            return _etag("")

    def _read_body(self, note_id: str) -> str:
        return self._read_document(note_id)[0]

    def _read_document(self, note_id: str) -> tuple[str, dict[str, Any]]:
        try:
            raw = self._body_path(note_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return "", {}
        return _parse_frontmatter(raw)

    def _write_body(self, note_id: str, content: str, *, metadata: dict[str, Any] | None = None) -> None:
        path = self._body_path(note_id)
        if path.is_file():
            self._snapshot_revision(note_id, path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{note_id}.", suffix=".tmp", dir=path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(_serialize_frontmatter(metadata or {"id": note_id}, content))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _snapshot_revision(self, note_id: str, path: Path) -> None:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.control_root / "revisions" / note_id
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = directory / f"{timestamp}-{digest[:12]}.md"
        if not any(item.name.endswith(f"-{digest[:12]}.md") for item in directory.glob("*.md")):
            target.write_bytes(raw)
        for stale in sorted(directory.glob("*.md"), reverse=True)[50:]:
            stale.unlink(missing_ok=True)

    def _relations(self, note_id: str, content: str) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM note_nodes WHERE kind='note'").fetchall()
        current_row = next((row for row in rows if str(row["note_id"]) == note_id), None)
        current_name = str(current_row["name"]) if current_row is not None else ""
        by_key: dict[str, sqlite3.Row] = {}
        for row in rows:
            by_key[str(row["note_id"]).casefold()] = row
            by_key[str(row["name"]).casefold()] = row
            _, metadata = self._read_document(str(row["note_id"]))
            for alias in metadata.get("aliases", []):
                by_key[str(alias).casefold()] = row
        links: list[dict[str, Any]] = []
        linked_ids: set[str] = set()
        for label in _WIKI_LINK_RE.findall(content):
            target = label.strip()
            target_row = by_key.get(target.casefold())
            item = {"target": target, "resolved": target_row is not None}
            if target_row is not None:
                linked_id = str(target_row["note_id"])
                linked_ids.add(linked_id)
                item.update({"note_id": linked_id, "name": str(target_row["name"])})
            links.append(item)
        backlinks: list[dict[str, Any]] = []
        _, current_metadata = self._read_document(note_id)
        current_keys = {
            note_id.casefold(), current_name.casefold(),
            *(str(alias).casefold() for alias in current_metadata.get("aliases", [])),
        }
        for row in rows:
            other_id = str(row["note_id"])
            if other_id == note_id:
                continue
            other_content, _ = self._read_document(other_id)
            labels = {label.strip().casefold() for label in _WIKI_LINK_RE.findall(other_content)}
            if labels & current_keys:
                backlinks.append({"note_id": other_id, "name": str(row["name"]), "path": self._logical_path(other_id)})
        return {"links": links, "backlinks": backlinks}

    def reindex(self) -> dict[str, int]:
        """Rebuild the SQLite index from Workspace Markdown files."""
        with self._index_lock:
            return self._reindex_locked()

    def _reindex_locked(self) -> dict[str, int]:
        paths = list(self.notes_root.rglob("*"))
        for path in paths:
            self._validate_managed_path(path)
        directories = sorted(
            (path for path in paths if path.is_dir()),
            key=lambda path: len(path.relative_to(self.notes_root).parts),
        )
        files = [path for path in paths if path.is_file() and path.suffix.casefold() == ".md"]
        discovered: list[dict[str, Any]] = []
        discovered_ids: set[str] = set()
        folder_ids: dict[Path, str] = {}
        folder_rows: list[dict[str, Any]] = []
        for path in directories:
            relative = path.relative_to(self.notes_root)
            folder_id = "folder-" + hashlib.sha256(relative.as_posix().casefold().encode("utf-8")).hexdigest()[:24]
            folder_ids[path.resolve()] = folder_id
            stat = path.stat()
            folder_rows.append({
                "note_id": folder_id,
                "name": path.name,
                "parent_id": folder_ids.get(path.parent.resolve()),
                "file_path": relative.as_posix(),
                "created_at": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(timespec="seconds"),
                "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            })
        for path in files:
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            content, metadata = _parse_frontmatter(raw)
            relative_path = path.relative_to(self.notes_root).as_posix()
            fallback_id = "note-" + hashlib.sha256(relative_path.casefold().encode("utf-8")).hexdigest()[:24]
            note_id = str(metadata.get("id") or fallback_id)
            if note_id in discovered_ids:
                note_id = fallback_id
            discovered_ids.add(note_id)
            name = str(metadata.get("title") or path.stem)
            stat = path.stat()
            discovered.append({
                "note_id": note_id,
                "name": name,
                "parent_id": folder_ids.get(path.parent.resolve()),
                "file_path": relative_path,
                "created_at": str(metadata.get("created") or datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(timespec="seconds")),
                "updated_at": str(metadata.get("updated") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")),
            })
        with self._connect() as connection:
            connection.execute("DELETE FROM note_nodes")
            for item in folder_rows:
                connection.execute(
                    "INSERT INTO note_nodes(note_id,kind,name,parent_id,file_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (item["note_id"], "folder", item["name"], item["parent_id"], item["file_path"], item["created_at"], item["updated_at"]),
                )
            for item in discovered:
                connection.execute(
                    "INSERT OR REPLACE INTO note_nodes(note_id,kind,name,parent_id,file_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (item["note_id"], "note", item["name"], item["parent_id"], item["file_path"], item["created_at"], item["updated_at"]),
                )
        self._last_disk_signature = self._disk_signature()
        return {"notes": len(discovered), "folders": len(folder_rows)}

    def _sync_index_from_disk(self) -> None:
        """Refresh the rebuildable index after an external editor changes the Vault."""
        signature = self._disk_signature()
        if signature == self._last_disk_signature:
            return
        with self._index_lock:
            signature = self._disk_signature()
            if signature != self._last_disk_signature:
                self._reindex_locked()

    def _remember_disk_signature(self) -> None:
        with self._index_lock:
            self._last_disk_signature = self._disk_signature()

    def _disk_signature(self) -> tuple[tuple[str, int, int, bool], ...]:
        result: list[tuple[str, int, int, bool]] = []
        for path in self.notes_root.rglob("*"):
            try:
                self._validate_managed_path(path)
                info = path.stat()
            except FileNotFoundError:
                continue
            result.append((
                path.relative_to(self.notes_root).as_posix(),
                info.st_mtime_ns,
                info.st_size,
                path.is_dir(),
            ))
        return tuple(sorted(result))

    @staticmethod
    def _clean_name(value: str, *, default: str) -> str:
        value = " ".join(value.strip().split()).replace("/", "／").replace("\\", "＼")
        return value[:240] or default

    @staticmethod
    def _validate_parent(connection: sqlite3.Connection, parent_id: str | None) -> None:
        if parent_id is None:
            return
        row = NoteStore._require(connection, parent_id)
        if row["kind"] != "folder":
            raise ValueError("Note parent must be a folder")

    @staticmethod
    def _is_descendant(connection: sqlite3.Connection, candidate: str | None, ancestor: str) -> bool:
        while candidate:
            if candidate == ancestor:
                return True
            row = connection.execute("SELECT parent_id FROM note_nodes WHERE note_id=?", (candidate,)).fetchone()
            candidate = str(row["parent_id"]) if row and row["parent_id"] else None
        return False

    @staticmethod
    def _require(connection: sqlite3.Connection, note_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM note_nodes WHERE note_id=?", (note_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown note: {note_id}")
        return row

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _etag(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _parse_frontmatter(raw: str) -> tuple[str, dict[str, Any]]:
    if not raw.startswith("---\n"):
        return raw, {}
    marker = raw.find("\n---", 4)
    if marker < 0:
        return raw, {}
    header = raw[4:marker].splitlines()
    body = raw[marker + 4:]
    if body.startswith("\n"):
        body = body[1:]
    metadata: dict[str, Any] = {"tags": [], "aliases": []}
    list_key: str | None = None
    for line in header:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"tags:", "aliases:"}:
            list_key = stripped[:-1]
            continue
        if list_key and stripped.startswith("-"):
            metadata.setdefault(list_key, []).append(stripped[1:].strip())
            continue
        list_key = None
        key, separator, value = stripped.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip("\"'")
    return body, metadata


def _serialize_frontmatter(metadata: dict[str, Any], content: str) -> str:
    lines = ["---"]
    for key in ("id", "title", "created", "updated"):
        if metadata.get(key) is not None:
            value = str(metadata[key]).replace("\n", " ").strip()
            lines.append(f"{key}: {value}")
    if metadata.get("parent_id"):
        lines.append(f"parent_id: {metadata['parent_id']}")
    lines.append("tags:")
    for tag in metadata.get("tags", []) or []:
        clean = str(tag).replace("\n", " ").strip()
        if clean:
            lines.append(f"  - {clean}")
    aliases = [str(value).replace("\n", " ").strip() for value in metadata.get("aliases", []) or []]
    aliases = [value for value in aliases if value]
    if aliases:
        lines.append("aliases:")
        lines.extend(f"  - {value}" for value in aliases)
    lines.extend(["---", ""])
    return "\n".join(lines) + content


def _clean_tags(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        clean = " ".join(str(value).strip().split())[:80]
        if clean and clean.casefold() not in {item.casefold() for item in result}:
            result.append(clean)
    return result[:64]


def _safe_file_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(".")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = f"_{value}"
    return value[:180] or "未命名笔记"
