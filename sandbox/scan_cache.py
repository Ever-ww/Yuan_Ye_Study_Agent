"""Rebuildable workspace safety scan cache.

The cache is an optimization, never an authorization grant.  Directory entry
changes, Git-visible changes, policy/backend changes or malformed cache data
force a complete policy scan before an OS sandbox may be created.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from .policy import NativePolicy


_CACHE_VERSION = 1
_POLICY_VERSION = "native-policy-v2"


class WorkspaceScanCache:
    def __init__(self, workspace: Path, state_root: Path, *, backend: str) -> None:
        self.workspace = workspace.resolve()
        self.state_root = state_root.resolve()
        identity = hashlib.sha256(str(self.workspace).casefold().encode("utf-8")).hexdigest()
        self.path = self.state_root / ".yy" / "sandbox" / "scan-cache" / f"{identity}.json"
        self.backend = backend
        self.last_cache_hit = False

    def protected_paths(self, policy: NativePolicy) -> tuple[tuple[Path, bool], ...]:
        cached = self._load()
        decoded = self._decode_protected(cached) if cached is not None else None
        if cached is not None and decoded is not None and self._is_current(cached, policy):
            self.last_cache_hit = True
            return decoded
        self.last_cache_hit = False
        protected = policy.protected_paths()
        directories = self._directory_stamps()
        value = {
            "version": _CACHE_VERSION,
            "policy_version": _POLICY_VERSION,
            "backend": self.backend,
            "workspace": str(self.workspace),
            "readable_roots": [str(path) for path in policy.readable_roots],
            "git_fingerprint": self._git_fingerprint(),
            "directories": directories,
            "protected": [
                {
                    "path": path.relative_to(self.workspace).as_posix(),
                    "hidden": hidden,
                }
                for path, hidden in protected
            ],
        }
        self._write(value)
        return protected

    def invalidate(self) -> None:
        self.path.unlink(missing_ok=True)

    def _is_current(self, value: dict[str, object], policy: NativePolicy) -> bool:
        try:
            if (
                value["version"] != _CACHE_VERSION
                or value["policy_version"] != _POLICY_VERSION
                or value["backend"] != self.backend
                or Path(str(value["workspace"])).resolve() != self.workspace
                or value["readable_roots"] != [str(path) for path in policy.readable_roots]
                or (current_git := self._git_fingerprint()) == "git-status-unavailable"
                or value["git_fingerprint"] != current_git
            ):
                return False
            stamps = value["directories"]
            if not isinstance(stamps, dict):
                return False
            # Entry creation/removal/replacement changes its containing
            # directory's mtime. Checking every known directory is much cheaper
            # than lstat/stat of every dependency file.
            for relative, expected in stamps.items():
                path = self.workspace if relative == "." else self.workspace / relative
                if path.stat().st_mtime_ns != int(expected):
                    return False
            return True
        except (KeyError, OSError, TypeError, ValueError):
            return False

    def _directory_stamps(self) -> dict[str, int]:
        selected: dict[str, int] = {}
        for directory, dirs, _files in os.walk(self.workspace, followlinks=False):
            base = Path(directory)
            relative = base.relative_to(self.workspace).as_posix() or "."
            selected[relative] = base.stat().st_mtime_ns
            # Canonical state/toolchain trees are boundary entries. Their own
            # contents are either hidden or separately authorized read-only.
            dirs[:] = [
                name for name in dirs
                if name not in {".git", ".yy", ".yy-backups", ".agents", ".codex", ".uv-cache", ".venv"}
            ]
        return selected

    def _git_fingerprint(self) -> str | None:
        if not (self.workspace / ".git").exists():
            return None
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            return "git-status-unavailable"
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.workspace,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        ).stdout
        return hashlib.sha256(head + b"\0" + result.stdout).hexdigest()

    def _load(self) -> dict[str, object] | None:
        try:
            if self.path.is_symlink():
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _write(self, value: dict[str, object]) -> None:
        relative_parent = self.path.parent.relative_to(self.state_root)
        cursor = self.state_root
        for part in relative_parent.parts:
            cursor = cursor / part
            if cursor.exists() and cursor.is_symlink():
                raise OSError(f"Sandbox scan cache path is a symlink: {cursor}")
            cursor.mkdir(exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _decode_protected(
        self, value: dict[str, object],
    ) -> tuple[tuple[Path, bool], ...] | None:
        items = value.get("protected")
        if not isinstance(items, list):
            return None
        selected: list[tuple[Path, bool]] = []
        for item in items:
            if not isinstance(item, dict):
                return None
            raw = item.get("path")
            hidden = item.get("hidden")
            if not isinstance(raw, str) or type(hidden) is not bool:
                return None
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts:
                return None
            path = (self.workspace / relative).resolve()
            if not path.is_relative_to(self.workspace):
                return None
            selected.append((path, hidden))
        return tuple(selected)


__all__ = ["WorkspaceScanCache"]
