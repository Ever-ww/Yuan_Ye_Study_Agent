"""Immutable native command permissions; never derived from model arguments."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .session import SandboxUnavailableError

# uv installs use hardlinks into this cache. Hide the cache entirely rather than
# treating its package objects as writable source (or allowing arbitrary aliases).
PROTECTED = frozenset({".git", ".yy", ".yy-backups", ".agents", ".codex", ".uv-cache"})
READ_ONLY = frozenset({".venv"})


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


@dataclass(frozen=True)
class NativePolicy:
    workspace: Path
    readable_roots: tuple[Path, ...] = ()

    def validate(self) -> None:
        root = self.workspace
        if not root.is_dir() or root == Path(root.anchor) or root == Path.home().resolve():
            raise SandboxUnavailableError("Sandbox workspace must be a dedicated project directory", reason_code="unsafe_workspace")
        for path in (root, *root.parents):
            if is_link(path):
                raise SandboxUnavailableError("Sandbox workspace has a symlink/reparse ancestor", reason_code="unsafe_workspace")
        for path in self.readable_roots:
            if not path.is_dir() or is_link(path) or path == Path(path.anchor) or path == Path.home().resolve():
                raise SandboxUnavailableError("Invalid explicit read root", reason_code="unsafe_read_root")
            if path.is_relative_to(root) or root.is_relative_to(path):
                raise SandboxUnavailableError("Read roots must not overlap the writable workspace; .venv is handled separately", reason_code="overlapping_read_root")

    def protected_paths(self) -> tuple[tuple[Path, bool], ...]:
        """Freeze each launch's carveouts. Links/sockets/hardlinks cannot alias host data."""
        found: list[tuple[Path, bool]] = []
        for directory, dirs, files in os.walk(self.workspace, followlinks=False):
            base = Path(directory)
            for name in list(dirs) + files:
                path = base / name
                if name in PROTECTED or name == ".env" or name.startswith(".env.") or name in {"settings.local.json", "credentials.json"}:
                    if is_link(path):
                        raise SandboxUnavailableError("Protected path is a link", reason_code="unsafe_protected_path")
                    found.append((path, True))
                    if name in dirs:
                        dirs.remove(name)
                elif name in READ_ONLY or (name == "target" and (base / "Cargo.toml").is_file()):
                    if is_link(path):
                        raise SandboxUnavailableError("Read-only toolchain must not be a link", reason_code="unsafe_toolchain")
                    found.append((path, False))
                    if name in dirs:
                        dirs.remove(name)
                elif is_link(path) or (path.is_file() and path.stat().st_nlink > 1) or not (path.is_file() or path.is_dir()):
                    raise SandboxUnavailableError(
                        f"Workspace links, hardlinks and special files require review: {path.relative_to(self.workspace)}",
                        reason_code="unsafe_workspace_entry",
                    )
        return tuple(sorted(found, key=lambda item: str(item[0])))
