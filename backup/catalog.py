"""Durability classification and safe Agent Home traversal."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from .models import DurabilityClass


class UnsafeArchiveEntryError(RuntimeError):
    pass


class AgentHomeDurabilityCatalog:
    """Explicit registry. Unknown files are protected as canonical."""

    _TRANSIENT_FILES = {
        ".yy/gateway/instance.json",
        ".yy/gateway/instance.lock",
        ".yy/gateway/startup.lock",
        ".yy/gateway/stop.request",
        ".yy/gateway/empty-secret",
    }
    _TRANSIENT_DIRS = {
        ".yy/uv-cache",
        ".yy/sandbox/docker",
        ".yy/harness-evolution/worktrees",
    }
    _REBUILDABLE_SUFFIXES = {".events.idx"}

    def classify(self, relative: PurePosixPath) -> DurabilityClass:
        value = relative.as_posix().lstrip("./")
        if value in self._TRANSIENT_FILES:
            return DurabilityClass.TRANSIENT
        if any(value == prefix or value.startswith(prefix + "/") for prefix in self._TRANSIENT_DIRS):
            return DurabilityClass.TRANSIENT
        if value.endswith(("-wal", "-shm")):
            return DurabilityClass.TRANSIENT
        if value.endswith(".partial"):
            return DurabilityClass.TRANSIENT
        if any(value.endswith(suffix) for suffix in self._REBUILDABLE_SUFFIXES):
            return DurabilityClass.REBUILDABLE
        return DurabilityClass.CANONICAL

    def iter_files(self, home: Path):
        home = home.resolve()
        if not home.is_dir():
            raise FileNotFoundError(home)
        for root, directories, files in os.walk(home, topdown=True, followlinks=False):
            root_path = Path(root)
            kept: list[str] = []
            for name in sorted(directories):
                child = root_path / name
                self._reject_special(child)
                relative = PurePosixPath(child.relative_to(home).as_posix())
                if self.classify(relative) is not DurabilityClass.TRANSIENT:
                    kept.append(name)
            directories[:] = kept
            for name in sorted(files):
                child = root_path / name
                self._reject_special(child)
                relative = PurePosixPath(child.relative_to(home).as_posix())
                durability = self.classify(relative)
                if durability is not DurabilityClass.TRANSIENT:
                    yield child, relative, durability

    @staticmethod
    def validate_member_name(name: str) -> PurePosixPath:
        if not name or "\x00" in name or "\\" in name:
            raise UnsafeArchiveEntryError("归档路径为空、包含 NUL 或反斜杠")
        value = PurePosixPath(name)
        if value.is_absolute() or ".." in value.parts:
            raise UnsafeArchiveEntryError(f"Restore 路径越界：{name}")
        if value.parts and (":" in value.parts[0] or value.parts[0] in {".", ""}):
            raise UnsafeArchiveEntryError(f"Restore 路径包含盘符或无效前缀：{name}")
        return value

    @staticmethod
    def _reject_special(path: Path) -> None:
        info = path.lstat()
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            raise UnsafeArchiveEntryError(f"不允许归档符号链接：{path}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise UnsafeArchiveEntryError(f"不允许归档特殊文件：{path}")


__all__ = ["AgentHomeDurabilityCatalog", "UnsafeArchiveEntryError"]
