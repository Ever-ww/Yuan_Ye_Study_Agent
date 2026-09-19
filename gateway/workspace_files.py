"""Safe Web workspace I/O over logical paths and existing sandbox primitives."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

from sandbox import CheckpointStore, PathMappingSnapshot, WorkspaceLockManager


class WorkspaceFileConflict(RuntimeError):
    def __init__(self, metadata: dict[str, object]) -> None:
        super().__init__("Workspace file changed since it was opened")
        self.metadata = metadata


class WorkspaceFileService:
    _PROTECTED_TOP_LEVEL = {".git", ".yy", ".yy-latex-build", ".agents", ".codex"}
    def __init__(self, agent_root: Path, agent_source_root: Path) -> None:
        self.agent_root = agent_root.resolve()
        self.agent_source_root = agent_source_root.resolve()

    async def tree(
        self, workspace_root: Path, logical_path: str = "YYWorkspace:\\",
        *, cursor: str | None = None, limit: int = 200,
    ) -> dict[str, object]:
        mapper, locks = self._context(workspace_root)
        directory = self._resolve(mapper, logical_path)
        if not directory.is_dir():
            raise NotADirectoryError(logical_path)
        async with locks.read(directory):
            entries = sorted(
                (item for item in directory.iterdir() if not self._is_protected(mapper, item)),
                key=lambda item: (not item.is_dir(), item.name.casefold()),
            )
            if cursor:
                entries = [item for item in entries if item.name.casefold() > cursor.casefold()]
            page = entries[:limit]
            result = [self._entry(mapper, item) for item in page]
        return {
            "path": mapper.to_logical_path(directory),
            "entries": result,
            "next_cursor": page[-1].name if len(entries) > limit and page else None,
        }

    async def read(self, workspace_root: Path, logical_path: str) -> dict[str, object]:
        mapper, locks = self._context(workspace_root)
        path = self._resolve(mapper, logical_path)
        if not path.is_file():
            raise FileNotFoundError(logical_path)
        self._ensure_regular(path)
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("Text editor files are limited to 5 MiB")
        async with locks.read(path):
            raw = path.read_bytes()
        if b"\0" in raw:
            raise ValueError("Binary files must be opened through the raw endpoint")
        return {
            **self._entry(mapper, path),
            "content": raw.decode("utf-8"),
            "etag": hashlib.sha256(raw).hexdigest(),
        }

    async def write(
        self, workspace_root: Path, logical_path: str, content: str,
        *, expected_etag: str | None,
    ) -> dict[str, object]:
        mapper, locks = self._context(workspace_root)
        path = self._resolve(mapper, logical_path)
        if path.exists() and not path.is_file():
            raise IsADirectoryError(logical_path)
        if path.exists(): self._ensure_regular(path)
        if not path.parent.is_dir():
            raise FileNotFoundError(mapper.to_logical_path(path.parent))
        async with locks.write(path):
            current = self._metadata(mapper, path) if path.exists() else None
            current_etag = str(current["etag"]) if current else None
            if current_etag != expected_etag:
                raise WorkspaceFileConflict(current or {
                    "path": mapper.to_logical_path(path), "etag": None, "exists": False,
                })
            raw = content.encode("utf-8")
            temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return {**self._entry(mapper, path), "etag": hashlib.sha256(raw).hexdigest()}

    async def create_entry(
        self, workspace_root: Path, logical_path: str, *, kind: str,
    ) -> dict[str, object]:
        mapper, locks = self._context(workspace_root)
        path = self._resolve(mapper, logical_path)
        if path.exists():
            raise FileExistsError(logical_path)
        if not path.parent.is_dir():
            raise FileNotFoundError(mapper.to_logical_path(path.parent))
        async with locks.write(path):
            if kind == "directory":
                path.mkdir()
            elif kind == "file":
                with path.open("xb") as handle:
                    handle.flush(); os.fsync(handle.fileno())
            else:
                raise ValueError("kind must be file or directory")
        return self._entry(mapper, path)

    async def move(
        self, workspace_root: Path, source_path: str, destination_path: str,
    ) -> dict[str, object]:
        mapper, locks = self._context(workspace_root)
        source = self._resolve(mapper, source_path)
        destination = self._resolve(mapper, destination_path)
        if not source.exists():
            raise FileNotFoundError(source_path)
        if destination.exists():
            raise FileExistsError(destination_path)
        if not destination.parent.is_dir():
            raise FileNotFoundError(mapper.to_logical_path(destination.parent))
        async with locks.write(source):
            os.replace(source, destination)
        return {"source": source_path, "entry": self._entry(mapper, destination)}

    async def delete(self, workspace_root: Path, logical_path: str) -> dict[str, object]:
        mapper, locks = self._context(workspace_root)
        path = self._resolve(mapper, logical_path)
        if path == mapper.workspace_root:
            raise PermissionError("Workspace root cannot be deleted")
        if not path.exists():
            raise FileNotFoundError(logical_path)
        checkpoints = CheckpointStore(mapper.workspace_root, state_root=self.agent_root)
        checkpoints.open("web-workspace")
        async with locks.write(path):
            before = checkpoints.create("web_delete_baseline", {"path": logical_path}, force=True)
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
            after = checkpoints.create("web_delete", {"path": logical_path}, force=True)
        return {
            "deleted": True,
            "path": logical_path,
            "checkpoint_id": before.commit_sha if before else None,
            "result_checkpoint_id": after.commit_sha if after else None,
        }

    def changes(self, workspace_root: Path) -> dict[str, object]:
        mapper, _ = self._context(workspace_root)
        completed = subprocess.run(
            ["git", "-C", str(mapper.workspace_root), "status", "--porcelain=v1", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            return {"available": False, "changes": []}
        changes = []
        for record in completed.stdout.split("\0"):
            if not record: continue
            status, _, relative = record.partition(" ")
            logical_relative = relative.replace("/", "\\")
            changes.append({"status": status.strip(), "path": f"YYWorkspace:\\{logical_relative}"})
        return {"available": True, "changes": changes}

    def raw_path(self, workspace_root: Path, logical_path: str) -> tuple[Path, dict[str, object]]:
        mapper, _ = self._context(workspace_root)
        path = self._resolve(mapper, logical_path)
        if not path.is_file(): raise FileNotFoundError(logical_path)
        self._ensure_regular(path)
        return path, self._metadata(mapper, path)

    def _context(self, workspace_root: Path) -> tuple[PathMappingSnapshot, WorkspaceLockManager]:
        root = workspace_root.resolve()
        mapper = PathMappingSnapshot(workspace_root=root, agent_source_root=self.agent_source_root)
        return mapper, WorkspaceLockManager(root, state_root=self.agent_root)

    def _resolve(self, mapper: PathMappingSnapshot, logical_path: str) -> Path:
        path = mapper.resolve_workspace_path(logical_path)
        if self._is_protected(mapper, path):
            raise PermissionError("Workspace control directories are not exposed by the file API")
        return path

    def _is_protected(self, mapper: PathMappingSnapshot, path: Path) -> bool:
        try:
            relative = path.resolve(strict=False).relative_to(mapper.workspace_root)
        except ValueError:
            return True
        return bool(relative.parts and relative.parts[0].casefold() in self._PROTECTED_TOP_LEVEL)

    def _entry(self, mapper: PathMappingSnapshot, path: Path) -> dict[str, object]:
        info = path.lstat()
        blocked = (
            stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)
        )
        kind = "blocked_link" if blocked else "directory" if path.is_dir() else "file"
        if blocked:
            logical_path = mapper.to_logical_path(path.parent).rstrip("/\\") + "\\" + path.name
        else:
            logical_path = mapper.to_logical_path(path)
        result: dict[str, object] = {
            "name": path.name, "path": logical_path, "kind": kind,
            "size": 0 if kind == "directory" else info.st_size,
            "modified_at": info.st_mtime_ns, "blocked": blocked,
        }
        return result

    def _metadata(self, mapper: PathMappingSnapshot, path: Path) -> dict[str, object]:
        return {**self._entry(mapper, path), "exists": True, "etag": self._etag(path)}

    @staticmethod
    def _etag(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""): digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _ensure_regular(path: Path) -> None:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            or info.st_nlink > 1
        ):
            raise PermissionError("Workspace file is a link, reparse point, or hard link")
