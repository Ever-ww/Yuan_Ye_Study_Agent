"""Trace-local logical paths for model-visible filesystem access.

Logical paths are a naming layer, not an authorization boundary.  Callers must
still pass the resolved host path through the applicable Tool and OS sandbox
policy before performing I/O.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class LogicalRoot(str, Enum):
    WORKSPACE = "workspace"
    AGENT_SOURCE = "agent_source"
    SKILLS = "skills"
    HOOKS = "hooks"


_WINDOWS_NAMES = {
    "yyworkspace": LogicalRoot.WORKSPACE,
    "yyagentsource": LogicalRoot.AGENT_SOURCE,
    "yyskills": LogicalRoot.SKILLS,
    "yyhooks": LogicalRoot.HOOKS,
}
_POSIX_NAMES = {
    "workspace": LogicalRoot.WORKSPACE,
    "agent-source": LogicalRoot.AGENT_SOURCE,
    "skills": LogicalRoot.SKILLS,
    "hooks": LogicalRoot.HOOKS,
}
_WINDOWS_PREFIX = re.compile(
    r"^(YYWorkspace|YYAgentSource|YYSkills|YYHooks):(?:[\\/](.*))?$",
    re.IGNORECASE,
)
_ENV_REFERENCE = re.compile(r"(^|[\\/])~(?:[\\/]|$)|\$\{?\w|%[^%]+%")


@dataclass(frozen=True, slots=True)
class PathMappingSnapshot:
    """Immutable logical-to-host mapping frozen for one Runtime/Trace.

    ``YYSkills`` and ``YYHooks`` are deliberately derived from
    ``YYAgentSource``.  They cannot be configured independently and therefore
    cannot accidentally keep pointing at the primary checkout while a Harness
    Runtime is operating in a worktree.
    """

    workspace_root: Path
    agent_source_root: Path
    trace_id: str = ""
    generation_id: str = ""
    platform: str = os.name
    skills_relative: str = "skills"
    hooks_relative: str = "extension/hook"

    def __post_init__(self) -> None:
        workspace = _validated_root(self.workspace_root, "workspace")
        source = _validated_root(self.agent_source_root, "agent source")
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "agent_source_root", source)

    @property
    def skills_root(self) -> Path:
        return self.agent_source_root / self.skills_relative

    @property
    def hooks_root(self) -> Path:
        return self.agent_source_root / self.hooks_relative

    def host_root(self, root: LogicalRoot) -> Path:
        return {
            LogicalRoot.WORKSPACE: self.workspace_root,
            LogicalRoot.AGENT_SOURCE: self.agent_source_root,
            LogicalRoot.SKILLS: self.skills_root,
            LogicalRoot.HOOKS: self.hooks_root,
        }[root]

    def shell_root(self, root: LogicalRoot) -> str:
        if self.platform == "nt" or self.platform == "win32":
            return {
                LogicalRoot.WORKSPACE: "YYWorkspace:\\",
                LogicalRoot.AGENT_SOURCE: "YYAgentSource:\\",
                LogicalRoot.SKILLS: "YYSkills:\\",
                LogicalRoot.HOOKS: "YYHooks:\\",
            }[root]
        return {
            LogicalRoot.WORKSPACE: "/yy/workspace",
            LogicalRoot.AGENT_SOURCE: "/yy/agent-source",
            LogicalRoot.SKILLS: "/yy/skills",
            LogicalRoot.HOOKS: "/yy/hooks",
        }[root]

    def resolve_workspace_path(self, requested: str) -> Path:
        allowed = [LogicalRoot.WORKSPACE]
        # Harness worktrees intentionally use the same host root for the task
        # workspace and the Agent source.  In that case the source aliases do
        # not expand filesystem authority; they are alternate names for paths
        # already inside the writable sandbox root.
        if self.agent_source_root == self.workspace_root:
            allowed.extend((LogicalRoot.AGENT_SOURCE, LogicalRoot.SKILLS, LogicalRoot.HOOKS))
        return self.resolve_and_validate(requested, allowed_roots=tuple(allowed))

    def resolve_agent_source_path(self, requested: str) -> Path:
        return self.resolve_and_validate(
            requested,
            allowed_roots=(LogicalRoot.AGENT_SOURCE, LogicalRoot.SKILLS, LogicalRoot.HOOKS),
            default_root=LogicalRoot.AGENT_SOURCE,
        )

    def resolve_and_validate(
        self,
        requested: str,
        *,
        allowed_roots: tuple[LogicalRoot, ...],
        default_root: LogicalRoot = LogicalRoot.WORKSPACE,
    ) -> Path:
        root, parts = self._parse(requested, default_root=default_root)
        if root not in allowed_roots:
            raise PermissionError(f"Logical root is not allowed here: {root.value}")
        base = self.host_root(root)
        candidate = base.joinpath(*parts)
        _reject_link_traversal(base, parts)
        resolved = candidate.resolve(strict=False)
        if resolved != base and not resolved.is_relative_to(base):
            raise PermissionError("Path escapes its logical root")
        if resolved.name.startswith(".env"):
            raise PermissionError("Access to environment files is denied")
        return resolved

    def to_logical_path(
        self,
        host_path: Path | str,
        *,
        preferred_root: LogicalRoot | None = None,
    ) -> str:
        path = Path(host_path).resolve(strict=False)
        ordered = [preferred_root] if preferred_root is not None else []
        ordered.extend(
            root for root in (
                LogicalRoot.SKILLS,
                LogicalRoot.HOOKS,
                LogicalRoot.WORKSPACE,
                LogicalRoot.AGENT_SOURCE,
            )
            if root not in ordered
        )
        for root in ordered:
            base = self.host_root(root).resolve(strict=False)
            if path == base or path.is_relative_to(base):
                relative = path.relative_to(base).as_posix()
                prefix = self.shell_root(root)
                if not relative:
                    return prefix
                separator = "" if prefix.endswith(("/", "\\")) else ("\\" if ":" in prefix else "/")
                relative = relative.replace("/", "\\") if ":" in prefix else relative
                return f"{prefix}{separator}{relative}"
        raise PermissionError("Host path is outside the logical path snapshot")

    def sanitize_for_model(self, value: str) -> str:
        """Replace known host roots without changing internal audit evidence."""
        result = value
        roots = [
            (self.skills_root, LogicalRoot.SKILLS),
            (self.hooks_root, LogicalRoot.HOOKS),
            (self.workspace_root, LogicalRoot.WORKSPACE),
            (self.agent_source_root, LogicalRoot.AGENT_SOURCE),
        ]
        roots.sort(key=lambda item: len(str(item[0])), reverse=True)
        for host, logical in roots:
            replacement = self.shell_root(logical).rstrip("/\\")
            variants = {str(host), str(host).replace("\\", "/"), str(host).replace("/", "\\")}
            for candidate in sorted(variants, key=len, reverse=True):
                flags = re.IGNORECASE if self.platform in {"nt", "win32"} else 0
                result = re.sub(re.escape(candidate), lambda _: replacement, result, flags=flags)
        return result

    def translate_posix_command(self, command: str) -> str:
        """Bridge logical POSIX roots for Seatbelt, which has no mount namespace.

        The resulting command still runs inside the Seatbelt filesystem policy;
        this translation is only naming, never authorization.
        """
        result = command
        roots = (
            LogicalRoot.SKILLS,
            LogicalRoot.HOOKS,
            LogicalRoot.AGENT_SOURCE,
            LogicalRoot.WORKSPACE,
        )
        for root in roots:
            logical = self.shell_root(root)
            quoted = shlex.quote(str(self.host_root(root)))
            # Keep the expression explicit rather than interpreting arbitrary
            # command syntax. Only a complete fixed logical-root token changes.
            boundary = re.escape(logical) + r"(?=$|/|[\s;&|()<>'\"])"
            result = re.sub(boundary, lambda _: quoted, result)
        return result

    def with_agent_source(
        self,
        source_root: Path,
        *,
        trace_id: str | None = None,
        generation_id: str | None = None,
    ) -> "PathMappingSnapshot":
        return PathMappingSnapshot(
            workspace_root=self.workspace_root,
            agent_source_root=source_root,
            trace_id=self.trace_id if trace_id is None else trace_id,
            generation_id=self.generation_id if generation_id is None else generation_id,
            platform=self.platform,
            skills_relative=self.skills_relative,
            hooks_relative=self.hooks_relative,
        )

    def _parse(
        self,
        requested: str,
        *,
        default_root: LogicalRoot,
    ) -> tuple[LogicalRoot, tuple[str, ...]]:
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("Path must be a non-empty string")
        raw = requested.strip()
        if _ENV_REFERENCE.search(raw):
            raise PermissionError("Home and environment-variable paths are not accepted")
        if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
            raise PermissionError("UNC and device paths are not accepted")
        matched = _WINDOWS_PREFIX.match(raw)
        if matched:
            root = _WINDOWS_NAMES[matched.group(1).casefold()]
            tail = matched.group(2) or ""
        elif raw == "/yy" or raw.startswith("/yy/"):
            chunks = raw.split("/")
            if len(chunks) < 3 or chunks[2] not in _POSIX_NAMES:
                raise PermissionError("Unknown logical path root")
            root = _POSIX_NAMES[chunks[2]]
            tail = "/".join(chunks[3:])
        else:
            # Native absolute paths are intentionally not accepted from model
            # arguments even when they happen to be inside an allowed root.
            if Path(raw).is_absolute() or re.match(r"^[A-Za-z]:", raw):
                raise PermissionError("Use a logical path instead of a host absolute path")
            root = default_root
            tail = raw
        parts = tuple(part for part in re.split(r"[\\/]", tail) if part not in {"", "."})
        if any(part == ".." for part in parts):
            raise PermissionError("Parent traversal is not accepted")
        return root, parts


def _validated_root(path: Path, label: str) -> Path:
    selected = Path(path)
    if not selected.is_absolute():
        raise ValueError(f"{label} root must be absolute")
    for candidate in (selected, *selected.parents):
        if candidate == Path(candidate.anchor):
            break
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError(f"{label} root must not have a symlink or reparse ancestor")
    resolved = selected.resolve(strict=False)
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError(f"{label} root must be a dedicated directory")
    return resolved


def _reject_link_traversal(base: Path, parts: tuple[str, ...]) -> None:
    cursor = base
    for part in parts:
        cursor = cursor / part
        if not cursor.exists() and not cursor.is_symlink():
            continue
        info = cursor.lstat()
        is_reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
        if stat.S_ISLNK(info.st_mode) or is_reparse:
            raise PermissionError("Logical path crosses a symlink or reparse point")
