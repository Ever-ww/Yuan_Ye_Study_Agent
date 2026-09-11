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

    def writable_roots(self, requested: tuple[str, ...] | None) -> tuple[Path, ...]:
        """Resolve model-declared write roots without treating them as grants.

        The Runtime already authorizes Bash.  These roots only narrow the OS
        backend's temporary filesystem permissions for that invocation/lease.
        Missing roots fall back to the workspace for backward compatibility.
        """
        if requested is None:
            return (self.workspace,)
        if not requested or len(requested) > 16:
            raise ValueError("writable_paths must contain between 1 and 16 directories")
        selected: list[Path] = []
        for raw in requested:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("writable_paths entries must be non-empty strings")
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Writable path must be workspace-relative: {raw}")
            path = (self.workspace / relative).resolve()
            if not path.is_relative_to(self.workspace) or not path.is_dir():
                raise ValueError(f"Writable path must be an existing workspace directory: {raw}")
            cursor = self.workspace
            for part in path.relative_to(self.workspace).parts:
                cursor = cursor / part
                if is_link(cursor):
                    raise SandboxUnavailableError(
                        f"Writable path crosses a link/reparse point: {raw}",
                        reason_code="unsafe_workspace_entry",
                    )
            if any(
                part in PROTECTED or part in READ_ONLY or part == ".env"
                or part.startswith(".env.")
                or part in {"settings.local.json", "credentials.json"}
                for part in path.relative_to(self.workspace).parts
            ):
                raise ValueError(f"Writable path is protected or read-only: {raw}")
            selected.append(path)
        # Keep a deterministic minimal set: a parent grant subsumes descendants.
        roots: list[Path] = []
        for path in sorted(set(selected), key=lambda item: (len(item.parts), str(item))):
            if not any(path == parent or path.is_relative_to(parent) for parent in roots):
                roots.append(path)
        return tuple(roots)
