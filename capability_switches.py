"""Workspace-scoped Skill and Tool switches for future Runtime turns."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4


_lock = RLock()
_tool_classes = (
    "ReadFileTool", "EditTool", "WriteTool", "BashTool", "SandboxRollbackTool",
    "SandboxCheckpointHistoryTool", "SandboxCheckpointBranchTool", "CalculatorTool",
    "SearchWorkspaceTool", "CurrentTimeTool", "ProfileReadTool", "BrowserUseTool",
    "WebSearchTool", "WebFetchTool", "PaperDownloadTool", "ReferenceSearchTool",
    "ReferenceGetTool", "ReferenceWriteTool", "PaperLibraryLookupTool",
    "PaperLibraryDownloadTool", "PaperLibraryReadTool", "PaperLibrarySaveTool",
    "SkillReadTool", "SkillInstallTool", "CronJobTool", "SubagentTool",
    "SessionHistoryTool", "SessionReadTool", "HarnessCapabilityTool",
)


def tool_catalog() -> tuple[dict[str, str], ...]:
    import tool  # Load the registry package before tool classes (their existing import order).
    import tools

    return tuple({"name": cls.name, "description": cls.description, "risk": cls.risk}
                 for cls in (getattr(tools, name) for name in _tool_classes))


def _path(workspace: Path) -> Path:
    return workspace / ".yy" / "capabilities.json"


def load_switches(workspace: Path) -> dict:
    path = _path(workspace)
    if path.parent.exists() and not path.parent.resolve().is_relative_to(workspace.resolve()):
        raise PermissionError("Capability settings must remain inside the Workspace")
    if path.is_symlink():
        raise ValueError("Capability settings must not be a symlink")
    if not path.exists():
        return {"revision": 0, "disabled_skills": [], "disabled_tools": []}
    if path.stat().st_size > 128 * 1024:
        raise ValueError("Capability settings are too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("revision")) is not int
        or data["revision"] < 0
        or any(not isinstance(data.get(key), list) or any(not isinstance(value, str) for value in data[key])
               for key in ("disabled_skills", "disabled_tools"))
    ):
        raise ValueError("Invalid Workspace capability settings")
    return data


def set_enabled(workspace: Path, kind: str, name: str, enabled: bool, expected_revision: int,
                *, skill_names: set[str]) -> dict:
    if kind not in {"skills", "tools"}:
        raise ValueError("Unknown capability kind")
    allowed = skill_names if kind == "skills" else {item["name"] for item in tool_catalog()}
    if name not in allowed:
        raise ValueError("Unknown Workspace capability")
    key = f"disabled_{kind}"
    with _lock:
        state = load_switches(workspace)
        if state["revision"] != expected_revision:
            raise CapabilityRevisionConflict(state["revision"])
        disabled = set(state[key])
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        if disabled == set(state[key]):
            return state
        state[key] = sorted(disabled)
        state["revision"] += 1
        path = _path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.resolve().is_relative_to(workspace.resolve()):
            raise PermissionError("Capability settings must remain inside the Workspace")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return state


class CapabilityRevisionConflict(Exception):
    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__("Capability settings changed; reload before retrying")
