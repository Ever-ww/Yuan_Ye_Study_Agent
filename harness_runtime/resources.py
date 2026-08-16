"""Fail-closed loading of Harness-only Tool and Skill resources."""

from __future__ import annotations

import hashlib
import importlib.util
import asyncio
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

from Agent.runtime.subagent import RuntimeSubagentRunner
from skill import SkillCatalogSnapshot
from skill.parser import catalog_xml, content_digest, parse_skill
from tool import AsyncToolRegistry, register_subagent
from tools import WebFetchTool, WebSearchTool
from tools.bash import BashTool
from tools.edit import EditTool
from tools.read_file import ReadFileTool
from tools.sandbox_rollback import SandboxRollbackTool
from tools.search_workspace import SearchWorkspaceTool
from tools.write import WriteTool

from .models import HarnessRuntimeProfile, HarnessRuntimeTrigger, content_hash


_TRIGGER_PROMPTS = {
    HarnessRuntimeTrigger.MANUAL: (
        "# MANUAL profile\nMaintain Hook behavior. Read the full repository, but write only "
        "extension/hook/** and tests/extensions/**. Use the controller-assigned test."
    ),
    HarnessRuntimeTrigger.ERROR: (
        "# ERROR profile\nRepair the concrete RuntimeFailure evidence. Git-tracked source and "
        "tests are writable, but runtime state, credentials, and user changes are permanently forbidden."
    ),
    HarnessRuntimeTrigger.CAPABILITY: (
        "# CAPABILITY profile\nAdd or repair a Tool capability. Write only tools/**, tests/tools/**, "
        "and the minimum approved Tool registration files. Preserve unrelated Tool contracts."
    ),
    HarnessRuntimeTrigger.DREAM: (
        "# DREAM profile\nPerform conservative, behavior-preserving optimization only inside the "
        "authorized changeset. Do not expand APIs, permissions, dependencies, or configuration."
    ),
}


class HarnessSkillReadTool:
    """Progressively load a frozen Skill once per trace; repeats return a stable reference."""

    name = "skill_read"
    description = "Read an authorized Harness Skill resource once in this frozen trace"
    risk = "read"
    idempotency = "PURE"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["name"],
    }

    def __init__(self, service: "HarnessRuntimeSkillService") -> None:
        self.service = service
        self._loaded: set[tuple[str, str, str, str]] = set()
        self.cache_hits = 0
        self.cache_misses = 0

    async def run(self, arguments: dict[str, Any], context) -> str:
        if not context.session_id:
            raise RuntimeError("skill_read requires an active Harness Session")
        snapshot = self.service.session_snapshot(context.session_id)
        name = str(arguments["name"])
        path = str(arguments.get("path") or "SKILL.md")
        key = (context.session_id, name, path, snapshot.digest)
        if key in self._loaded:
            self.cache_hits += 1
            entry = snapshot.by_name().get(name)
            if entry is None:
                raise KeyError(f"Skill is not authorized in this Harness trace: {name}")
            return f"skill-ref:{name}:{path}:{entry.content_digest}"
        content = await asyncio.to_thread(self.service.read, snapshot, name, path)
        self._loaded.add(key)
        self.cache_misses += 1
        return content


class HarnessRuntimeSkillService:
    """Read-only multi-root Skill catalog isolated from the main Agent SkillService."""

    def __init__(self, agent_root: Path, workspace_root: Path, roots: tuple[Path, ...]) -> None:
        self.agent_root = agent_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.source_root = self.workspace_root
        self.skills_root = Path("harness-runtime-skills")
        self._roots = tuple(root.resolve() for root in roots)
        self._locations: dict[str, Path] = {}
        self._session_snapshots: dict[str, SkillCatalogSnapshot] = {}
        self._catalog = self._load_catalog()

    def _load_catalog(self):
        values = []
        for parent in self._roots:
            if parent.is_symlink() or not parent.is_dir():
                raise ValueError(f"Invalid Harness Skill root: {parent}")
            for root in sorted(parent.iterdir(), key=lambda item: item.name):
                if root.name.startswith("."):
                    continue
                if root.is_symlink() or not root.is_dir():
                    raise ValueError(f"Invalid Harness Skill package: {root}")
                metadata = parse_skill(root)
                if metadata.name in self._locations:
                    raise ValueError(f"Duplicate Harness Skill name: {metadata.name}")
                self._locations[metadata.name] = root.resolve()
                values.append(metadata)
        return tuple(sorted(values, key=lambda item: item.name))

    def catalog(self):
        return self._catalog

    def catalog_snapshot(self) -> SkillCatalogSnapshot:
        payload = "\n".join(f"{item.name}:{item.content_digest}" for item in self._catalog)
        return SkillCatalogSnapshot(
            digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            skills=self._catalog,
        )

    def bind_session(self, session_id: str, snapshot: SkillCatalogSnapshot) -> None:
        if snapshot.digest != self.catalog_snapshot().digest:
            raise RuntimeError("Harness Skill snapshot does not match the frozen profile")
        self._session_snapshots[session_id] = snapshot

    def session_snapshot(self, session_id: str) -> SkillCatalogSnapshot:
        try:
            return self._session_snapshots[session_id]
        except KeyError as exc:
            raise RuntimeError("Harness Session has no frozen Skill catalog") from exc

    def unbind_session(self, session_id: str) -> None:
        self._session_snapshots.pop(session_id, None)

    def catalog_xml(self, snapshot: SkillCatalogSnapshot | None = None) -> str:
        selected = snapshot or self.catalog_snapshot()
        if selected.digest != self.catalog_snapshot().digest:
            raise RuntimeError("Harness Skill catalog changed during an active trace")
        return catalog_xml(selected.skills)

    def read(self, snapshot: SkillCatalogSnapshot, name: str, path: str = "SKILL.md") -> str:
        if snapshot.digest != self.catalog_snapshot().digest or name not in snapshot.by_name():
            raise KeyError(f"Skill is not authorized in this Harness trace: {name}")
        root = self._locations[name]
        if content_digest(root) != snapshot.by_name()[name].content_digest:
            raise RuntimeError("Harness Skill content changed; create a new trace")
        relative = PurePosixPath(path.replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise PermissionError("Harness Skill path must remain inside the selected package")
        candidate = root.joinpath(*relative.parts)
        cursor = candidate
        while cursor != root:
            if cursor.is_symlink():
                raise PermissionError("Harness Skill resources cannot use symlinks")
            cursor = cursor.parent
        target = candidate.resolve()
        if root != target and root not in target.parents:
            raise PermissionError("Harness Skill path escaped its package")
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError(path)
        raw = target.read_bytes()
        if len(raw) > 1024 * 1024 or b"\0" in raw:
            raise ValueError("Harness skill_read only supports UTF-8 text up to 1 MiB")
        return raw.decode("utf-8")


class HarnessRuntimeResourceLoader:
    def __init__(self, resource_root: Path) -> None:
        self.resource_root = resource_root.resolve()
        if self.resource_root.is_symlink() or not self.resource_root.is_dir():
            raise ValueError(f"Harness runtime resources are unavailable: {resource_root}")

    def profile(self, trigger: HarnessRuntimeTrigger | str) -> HarnessRuntimeProfile:
        selected = trigger if isinstance(trigger, HarnessRuntimeTrigger) else HarnessRuntimeTrigger(trigger)
        tool_base = self.resource_root / "tools"
        skill_base = self.resource_root / "skills"
        roots = (
            tool_base / "common",
            tool_base / selected.value,
            skill_base / "common",
            skill_base / selected.value,
        )
        for root in roots:
            self._validate_child(root, tool_base if root in roots[:2] else skill_base)
        return HarnessRuntimeProfile(
            trigger=selected,
            resource_root=self.resource_root,
            tool_roots=roots[:2],
            skill_roots=roots[2:],
            stable_instructions=_TRIGGER_PROMPTS[selected],
        )

    @staticmethod
    def _validate_child(path: Path, parent: Path) -> None:
        resolved_parent = parent.resolve()
        resolved = path.resolve()
        if path.is_symlink() or not path.is_dir() or resolved.parent != resolved_parent:
            raise ValueError(f"Harness resource root is invalid or escaped: {path}")

    def build_skills(
        self,
        profile: HarnessRuntimeProfile,
        *,
        agent_root: Path,
        workspace_root: Path,
    ) -> HarnessRuntimeSkillService:
        self._validate_profile(profile)
        return HarnessRuntimeSkillService(agent_root, workspace_root, profile.skill_roots)

    def build_tools(self, profile: HarnessRuntimeProfile, config, skills) -> AsyncToolRegistry:
        self._validate_profile(profile)
        web_fetch = WebFetchTool(
            timeout_seconds=config.web_fetch_timeout_seconds,
            max_bytes=config.web_fetch_max_bytes,
            max_chars=config.web_fetch_max_chars,
            use_system_proxy=config.use_system_proxy,
            proxy_url=config.proxy_url,
        )
        values: list[Any] = [
            ReadFileTool(),
            SearchWorkspaceTool(),
            EditTool(),
            WriteTool(),
            BashTool(),
            SandboxRollbackTool(),
            web_fetch,
            HarnessSkillReadTool(skills),
        ]
        if config.web_search_api_key:
            values.append(WebSearchTool(
                config.web_search_api_key,
                timeout_seconds=config.web_search_timeout_seconds,
                use_system_proxy=config.use_system_proxy,
                proxy_url=config.proxy_url,
            ))
        for root in profile.tool_roots:
            module = self._load_tool_package(root, profile.trigger)
            factory = getattr(module, "build_tools", None)
            if not callable(factory):
                raise ValueError(f"Harness Tool package lacks build_tools(): {root}")
            built = factory(profile.trigger)
            if not isinstance(built, (list, tuple)):
                raise ValueError(f"Harness Tool package returned an invalid collection: {root}")
            values.extend(built)
        values.sort(key=lambda item: item.name)
        registry = AsyncToolRegistry(values)
        register_subagent(registry, RuntimeSubagentRunner(config, registry))
        forbidden = {
            "harness_capability", "harness_evolve", "harness_manual",
            "harness_error", "harness_dream", "skill_install", "cronjob",
        }
        leaked = forbidden.intersection(registry.names())
        if leaked:
            raise RuntimeError(f"Outer Harness entry leaked into Coding Runtime: {sorted(leaked)[0]}")
        return registry

    def tool_catalog_hash(self, registry: AsyncToolRegistry) -> str:
        return content_hash(registry.contract_snapshot())

    def _validate_profile(self, profile: HarnessRuntimeProfile) -> None:
        if profile.resource_root.resolve() != self.resource_root:
            raise ValueError("Harness profile belongs to a different resource root")
        expected = self.profile(profile.trigger)
        if profile.tool_roots != expected.tool_roots or profile.skill_roots != expected.skill_roots:
            raise ValueError("Harness profile attempted to widen its resource roots")

    def _load_tool_package(self, root: Path, trigger: HarnessRuntimeTrigger) -> ModuleType:
        init = root / "__init__.py"
        if init.is_symlink() or not init.is_file() or init.resolve().parent != root.resolve():
            raise ValueError(f"Harness Tool package is invalid: {root}")
        expected_name = "common" if root.name == "common" else trigger.value
        if root.name != expected_name:
            raise PermissionError(f"Cross-trigger Harness Tool package denied: {root.name}")
        digest = hashlib.sha256(init.read_bytes()).hexdigest()[:16]
        module_name = f"yy_harness_tools_{root.name}_{digest}"
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(module_name, init)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load Harness Tool package: {root}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
