"""Immutable runtime-resource generations and hot-reload coordination.

The Gateway and durable runtime remain the stable core.  This module snapshots
the files which contribute tools, skills, prompts, and extension hooks, then
publishes an immutable generation through a SQLite CAS owned by
``StateController``.  A Runtime only ever reads a published snapshot.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import mkdtemp
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


class RuntimeProfile(str, Enum):
    INTERACTIVE = "interactive"
    CRON = "cron"
    DREAM = "dream"
    SUBAGENT = "subagent"
    COMPRESSION = "compression"
    MAINTENANCE = "maintenance"
    HARNESS_MANUAL = "harness:manual"
    HARNESS_ERROR = "harness:error"
    HARNESS_CAPABILITY = "harness:capability"
    HARNESS_DREAM = "harness:dream"


class RuntimeContributionKind(str, Enum):
    TOOL = "tool"
    SKILL = "skill"
    STABLE_PROMPT = "stable_prompt"
    HOOK = "hook"
    EXTENSION = "extension"
    DYNAMIC_CONTEXT = "dynamic_context"
    OBSERVER = "observer"


class RuntimePluginFailureKind(str, Enum):
    """Normalized attribution used by generation quarantine.

    Only implementation failures make a plugin version unhealthy.  Domain
    failures remain durable in their own Operation/Attempt ledger.
    """

    PLUGIN_EXCEPTION = "plugin_exception"
    PLUGIN_TIMEOUT = "plugin_timeout"
    CONTRACT_VIOLATION = "contract_violation"
    LOAD_FAILURE = "load_failure"
    BUSINESS_FAILURE = "business_failure"
    INVALID_ARGUMENTS = "invalid_arguments"
    PROVIDER_FAILURE = "provider_failure"
    POLICY_DENIAL = "policy_denial"

    @property
    def counts_toward_quarantine(self) -> bool:
        return self in {
            self.PLUGIN_EXCEPTION,
            self.PLUGIN_TIMEOUT,
            self.CONTRACT_VIOLATION,
            self.LOAD_FAILURE,
        }


class RuntimeResourceFile(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    root: str = Field(pattern=r"^(source|agent)$")
    path: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimePluginContribution(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    kind: RuntimeContributionKind
    name: str = Field(min_length=1, max_length=200)
    profiles: tuple[RuntimeProfile, ...]
    contract: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profiles", mode="before")
    @classmethod
    def _tuple_profiles(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class RuntimePluginDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    plugin_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
    plugin_version: str = Field(default="1", min_length=1, max_length=64)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    core_api_version: int = Field(default=1, ge=1)
    requires_plugins: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    contributions: tuple[RuntimePluginContribution, ...]
    files: tuple[RuntimeResourceFile, ...]

    @field_validator("requires_plugins", "conflicts_with", "contributions", "files", mode="before")
    @classmethod
    def _tuple_values(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class RuntimeResourceGeneration(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    generation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_generation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    core_api_version: int = Field(default=1, ge=1)
    descriptors: tuple[RuntimePluginDescriptor, ...]
    semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_root: Path

    @field_validator("descriptors", mode="before")
    @classmethod
    def _tuple_descriptors(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class RuntimeResourceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    generation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: RuntimeProfile
    descriptor_ids: tuple[str, ...]
    contributions: tuple["ResolvedRuntimeContribution", ...] = ()
    source_root: Path
    agent_root: Path
    tool_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    hook_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("descriptor_ids", mode="before")
    @classmethod
    def _tuple_ids(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("contributions", mode="before")
    @classmethod
    def _tuple_contributions(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class ResolvedRuntimeContribution(BaseModel):
    """One profile-authorized contribution frozen into a Runtime Snapshot."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    plugin_id: str
    plugin_version: str
    kind: RuntimeContributionKind
    name: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract: dict[str, Any] = Field(default_factory=dict)


RuntimeResourceSnapshot.model_rebuild()


class RuntimeReloadPlan(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: RuntimeResourceGeneration
    changed_plugins: tuple[str, ...]
    removed_plugins: tuple[str, ...] = ()
    approval_required: bool = False
    reasons: tuple[str, ...] = ()

    @field_validator("changed_plugins", "removed_plugins", "reasons", mode="before")
    @classmethod
    def _tuples(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class RuntimeReloadResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: str
    message: str
    generation_id: str | None = None
    plan_hash: str | None = None
    changed_plugins: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeResourceBundle:
    """Runtime-owned instances created from one immutable Snapshot.

    Generation is the persisted version, Snapshot is the profile/grant view,
    and Bundle is the only object the lifecycle bus mounts into a Runtime.
    Concrete registries remain responsible for lookup and execution.
    """

    snapshot: RuntimeResourceSnapshot
    tools: Any
    skills: Any
    prompts: Any
    hooks: Any
    extensions: Any
    contributions: tuple[ResolvedRuntimeContribution, ...] = ()
    contribution_instances: Mapping[RuntimeContributionKind, tuple[Any, ...]] | None = None

    def __post_init__(self) -> None:
        values = self.contribution_instances or {}
        object.__setattr__(
            self,
            "contribution_instances",
            MappingProxyType({key: tuple(items) for key, items in values.items()}),
        )

    def instances_for(self, kind: RuntimeContributionKind) -> tuple[Any, ...]:
        if self.contribution_instances is not None:
            return tuple(self.contribution_instances.get(kind, ()))
        instance = {
            RuntimeContributionKind.TOOL: self.tools,
            RuntimeContributionKind.SKILL: self.skills,
            RuntimeContributionKind.STABLE_PROMPT: self.prompts,
            RuntimeContributionKind.HOOK: self.hooks,
            RuntimeContributionKind.EXTENSION: self.extensions,
            RuntimeContributionKind.DYNAMIC_CONTEXT: None,
            RuntimeContributionKind.OBSERVER: None,
        }.get(kind)
        if instance is None:
            return ()
        return tuple(
            instance for contribution in self.contributions if contribution.kind is kind
        )


class RuntimeResourceMountTarget(Protocol):
    """Small stable-core seam used by the Hook lifecycle assembly bus."""

    def mount_runtime_resources(
        self, bundle: RuntimeResourceBundle, event: Any,
    ) -> None: ...

    def unmount_runtime_resources(
        self, bundle: RuntimeResourceBundle, event: Any,
    ) -> None: ...


def register_runtime_resource_callbacks(
    registry: Any,
    bundle: RuntimeResourceBundle,
    target: RuntimeResourceMountTarget,
) -> None:
    """Mount one immutable Bundle through the lifecycle bus.

    The callbacks only bind/freeze resources.  They do not discover plugins,
    mutate Generations, look up Tools, or execute any contribution.
    """
    from Agent.hook import HookPoint

    async def mount(event: Any) -> None:
        target.mount_runtime_resources(bundle, event)

    async def unmount(event: Any) -> None:
        target.unmount_runtime_resources(bundle, event)

    registry.register(
        HookPoint.TRACE_START,
        mount,
        priority=-10_000,
        identity=f"runtime-resources:{bundle.snapshot.generation_id}:mount",
    )
    registry.register(
        HookPoint.TRACE_END,
        unmount,
        priority=10_000,
        identity=f"runtime-resources:{bundle.snapshot.generation_id}:unmount",
    )


class RuntimeResourceProvider(Protocol):
    provider_id: str

    def discover(self) -> tuple[RuntimePluginDescriptor, ...]: ...


class StaticRuntimeResourceProvider:
    """Publish a stable-core adapter as a versioned capability contribution.

    The Memory store, sandbox backend and HookExecutor remain Core.  Their
    narrow Runtime adapters are selected by Generation/Profile and mounted by
    the lifecycle bus, so Runtime assembly no longer has a second implicit
    path for them.
    """

    def __init__(
        self,
        provider_id: str,
        contributions: tuple[RuntimePluginContribution, ...],
        *,
        contract_version: int = 1,
        requires_plugins: tuple[str, ...] = (),
    ) -> None:
        self.provider_id = provider_id
        self.contributions = contributions
        self.contract_version = contract_version
        self.requires_plugins = requires_plugins

    def discover(self) -> tuple[RuntimePluginDescriptor, ...]:
        identity = _canonical_json({
            "provider_id": self.provider_id,
            "contract_version": self.contract_version,
            "contributions": [item.model_dump(mode="json") for item in self.contributions],
        })
        digest = _sha256(identity)
        return (RuntimePluginDescriptor(
            plugin_id=self.provider_id,
            plugin_version=str(self.contract_version),
            source_hash=digest,
            semantic_hash=digest,
            requires_plugins=self.requires_plugins,
            contributions=self.contributions,
            files=(),
        ),)


def _semantic_bytes(path: Path, raw: bytes) -> bytes:
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(raw.decode("utf-8"), filename=str(path))
        except (UnicodeDecodeError, SyntaxError):
            return raw
        return ast.dump(tree, annotate_fields=True, include_attributes=False).encode("utf-8")
    if suffix == ".json":
        try:
            return _canonical_json(json.loads(raw.decode("utf-8"))).encode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        return raw
    return "\n".join(line.rstrip() for line in text.split("\n")).encode("utf-8")


class FileTreeRuntimeResourceProvider:
    """Describe one existing resource tree without prescribing plugin layout."""

    def __init__(
        self,
        provider_id: str,
        root: Path,
        *,
        root_kind: str,
        contribution_kind: RuntimeContributionKind,
        profiles: tuple[RuntimeProfile, ...],
        relative_to: Path,
        requires_plugins: tuple[str, ...] = (),
    ) -> None:
        self.provider_id = provider_id
        self.root = root.resolve()
        self.root_kind = root_kind
        self.kind = contribution_kind
        self.profiles = profiles
        self.relative_to = relative_to.resolve()
        self.requires_plugins = requires_plugins

    def discover(self) -> tuple[RuntimePluginDescriptor, ...]:
        if not self.root.exists():
            return ()
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError(f"Invalid runtime resource root: {self.root}")
        files: list[RuntimeResourceFile] = []
        raw_identity: list[str] = []
        semantic_identity: list[str] = []
        for path in sorted(self.root.rglob("*"), key=lambda item: item.as_posix()):
            relative_parts = path.relative_to(self.root).parts
            if "__pycache__" in relative_parts or path.suffix.lower() in {".pyc", ".pyo"}:
                continue
            if path.is_dir():
                if path.is_symlink():
                    raise ValueError(f"Runtime resource directory cannot be a symlink: {path}")
                continue
            if path.is_symlink():
                raise ValueError(f"Runtime resource file cannot be a symlink: {path}")
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(self.relative_to).as_posix()
            except ValueError as exc:
                raise ValueError(f"Runtime resource escaped its root: {path}") from exc
            raw = resolved.read_bytes()
            source_hash = _sha256(raw)
            semantic_hash = _sha256(_semantic_bytes(resolved, raw))
            files.append(RuntimeResourceFile(
                root=self.root_kind,
                path=relative,
                source_hash=source_hash,
                semantic_hash=semantic_hash,
            ))
            raw_identity.append(f"{self.root_kind}:{relative}:{source_hash}")
            semantic_identity.append(f"{self.root_kind}:{relative}:{semantic_hash}")
        source_hash = _sha256("\n".join(raw_identity))
        semantic_hash = _sha256("\n".join(semantic_identity))
        descriptor = RuntimePluginDescriptor(
            plugin_id=self.provider_id,
            source_hash=source_hash,
            semantic_hash=semantic_hash,
            requires_plugins=self.requires_plugins,
            contributions=(RuntimePluginContribution(
                kind=self.kind,
                name=self.provider_id,
                profiles=self.profiles,
            ),),
            files=tuple(files),
        )
        return (descriptor,)


def default_runtime_resource_providers(
    source_root: Path, agent_root: Path,
) -> tuple[RuntimeResourceProvider, ...]:
    source = source_root.resolve()
    agent = agent_root.resolve()
    interactive = (RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON, RuntimeProfile.SUBAGENT)
    harness_profiles = {
        "manual": RuntimeProfile.HARNESS_MANUAL,
        "error": RuntimeProfile.HARNESS_ERROR,
        "capability": RuntimeProfile.HARNESS_CAPABILITY,
        "dream": RuntimeProfile.HARNESS_DREAM,
    }
    providers: list[RuntimeResourceProvider] = [
        StaticRuntimeResourceProvider(
            "runtime.adapter.memory",
            (RuntimePluginContribution(
                kind=RuntimeContributionKind.HOOK,
                name="memory-retrieval-projection-adapter",
                profiles=(
                    RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON,
                    RuntimeProfile.MAINTENANCE, *tuple(harness_profiles.values()),
                ),
                contract={"adapter": "memory", "contract_version": 1},
            ),),
        ),
        StaticRuntimeResourceProvider(
            "runtime.adapter.dynamic-context",
            (RuntimePluginContribution(
                kind=RuntimeContributionKind.DYNAMIC_CONTEXT,
                name="provider-context-adapter",
                profiles=(
                    RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON,
                    RuntimeProfile.MAINTENANCE, *tuple(harness_profiles.values()),
                ),
                contract={"adapter": "dynamic_context", "contract_version": 1},
            ),),
            requires_plugins=("runtime.adapter.memory",),
        ),
        StaticRuntimeResourceProvider(
            "runtime.adapter.sandbox",
            (RuntimePluginContribution(
                kind=RuntimeContributionKind.HOOK,
                name="sandbox-policy-context-adapter",
                profiles=(
                    RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON,
                    *tuple(harness_profiles.values()),
                ),
                contract={"adapter": "sandbox", "contract_version": 1},
            ),),
        ),
        FileTreeRuntimeResourceProvider(
            "builtin.observer", source / "observer_plugins", root_kind="source",
            contribution_kind=RuntimeContributionKind.OBSERVER,
            profiles=(
                RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON, RuntimeProfile.DREAM,
                RuntimeProfile.HARNESS_MANUAL, RuntimeProfile.HARNESS_ERROR,
                RuntimeProfile.HARNESS_CAPABILITY, RuntimeProfile.HARNESS_DREAM,
            ),
            relative_to=source,
        ),
        FileTreeRuntimeResourceProvider(
            "builtin.tools", source / "tools", root_kind="source",
            contribution_kind=RuntimeContributionKind.TOOL, profiles=interactive,
            relative_to=source,
        ),
        FileTreeRuntimeResourceProvider(
            "builtin.skills", source / "skills", root_kind="source",
            contribution_kind=RuntimeContributionKind.SKILL,
            profiles=(RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON), relative_to=source,
        ),
        FileTreeRuntimeResourceProvider(
            "runtime.skills.interactive",
            source / "runtime-resources" / "interactive" / "skills",
            root_kind="source", contribution_kind=RuntimeContributionKind.SKILL,
            profiles=(RuntimeProfile.INTERACTIVE,), relative_to=source,
            requires_plugins=("builtin.skills",),
        ),
        FileTreeRuntimeResourceProvider(
            "runtime.skills.cron", source / "runtime-resources" / "cron" / "skills",
            root_kind="source", contribution_kind=RuntimeContributionKind.SKILL,
            profiles=(RuntimeProfile.CRON,), relative_to=source,
            requires_plugins=("builtin.skills",),
        ),
        FileTreeRuntimeResourceProvider(
            "runtime.skills.dream", source / "runtime-resources" / "dream" / "skills",
            root_kind="source", contribution_kind=RuntimeContributionKind.SKILL,
            profiles=(RuntimeProfile.DREAM,), relative_to=source,
        ),
        FileTreeRuntimeResourceProvider(
            "builtin.prompts", agent / ".yy" / "agents", root_kind="agent",
            contribution_kind=RuntimeContributionKind.STABLE_PROMPT,
            profiles=(RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON, RuntimeProfile.SUBAGENT),
            relative_to=agent,
        ),
        FileTreeRuntimeResourceProvider(
            "builtin.extensions", source / "extension", root_kind="source",
            contribution_kind=RuntimeContributionKind.EXTENSION,
            profiles=(RuntimeProfile.INTERACTIVE,), relative_to=source,
            requires_plugins=("builtin.tools",),
        ),
    ]
    harness_root = source / "harness-evolution" / "runtime"
    all_harness_profiles = tuple(harness_profiles.values())
    for kind, directory in (
        (RuntimeContributionKind.TOOL, "tools"),
        (RuntimeContributionKind.SKILL, "skills"),
    ):
        common_id = f"harness.{directory}.common"
        providers.append(FileTreeRuntimeResourceProvider(
            common_id,
            harness_root / directory / "common",
            root_kind="source",
            contribution_kind=kind,
            profiles=all_harness_profiles,
            relative_to=source,
        ))
        for trigger, profile in harness_profiles.items():
            providers.append(FileTreeRuntimeResourceProvider(
                f"harness.{directory}.{trigger}",
                harness_root / directory / trigger,
                root_kind="source",
                contribution_kind=kind,
                profiles=(profile,),
                relative_to=source,
                requires_plugins=(common_id,),
            ))
    return tuple(providers)


class RuntimePluginManager:
    """Build, persist, activate and resolve immutable resource generations."""

    CORE_API_VERSION = 1

    def __init__(
        self,
        *,
        source_root: Path,
        agent_root: Path,
        state_controller: Any,
        providers: Iterable[RuntimeResourceProvider] | None = None,
    ) -> None:
        self.source_root = source_root.resolve()
        self.agent_root = agent_root.resolve()
        self.state_controller = state_controller
        self.storage_root = self.agent_root / ".yy" / "runtime-plugins" / "generations"
        self.providers = tuple(
            providers or default_runtime_resource_providers(self.source_root, self.agent_root)
        )

    def _artifact_root(self, generation_id: str) -> Path:
        # Keep Windows workspaces below MAX_PATH while the manifest and SQLite
        # continue to carry the complete collision-resistant identity.
        return self.storage_root / generation_id[:24]

    def discover(self) -> tuple[RuntimePluginDescriptor, ...]:
        values = [descriptor for provider in self.providers for descriptor in provider.discover()]
        values.sort(key=lambda item: item.plugin_id)
        identities = [item.plugin_id for item in values]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate Runtime plugin id")
        by_id = {item.plugin_id: item for item in values}
        contribution_names: set[tuple[RuntimeProfile, RuntimeContributionKind, str]] = set()
        member_owners: dict[tuple[str, str], tuple[str, str]] = {}
        for item in values:
            if item.core_api_version != self.CORE_API_VERSION:
                raise ValueError(f"Unsupported core API for plugin {item.plugin_id}")
            missing = set(item.requires_plugins) - set(by_id)
            if missing:
                raise ValueError(f"Plugin {item.plugin_id} has missing dependency: {sorted(missing)[0]}")
            conflict = set(item.conflicts_with).intersection(by_id)
            if conflict:
                raise ValueError(f"Plugin {item.plugin_id} conflicts with {sorted(conflict)[0]}")
            exposed_profiles = {
                profile
                for contribution in item.contributions
                for profile in contribution.profiles
            }
            for required_id in item.requires_plugins:
                required_profiles = {
                    profile
                    for contribution in by_id[required_id].contributions
                    for profile in contribution.profiles
                }
                missing_profiles = exposed_profiles - required_profiles
                if missing_profiles:
                    raise ValueError(
                        f"Plugin {item.plugin_id} dependency {required_id} is not exposed to "
                        f"profile {sorted(value.value for value in missing_profiles)[0]}"
                    )
            for contribution in item.contributions:
                for profile in contribution.profiles:
                    identity = (profile, contribution.kind, contribution.name)
                    if identity in contribution_names:
                        raise ValueError(
                            f"Duplicate Runtime contribution {contribution.kind.value}/"
                            f"{contribution.name} in profile {profile.value}"
                        )
                    contribution_names.add(identity)
            for member in item.files:
                self._validate_hot_resource_member(member)
                identity = (member.root, member.path)
                owner = member_owners.get(identity)
                if owner is not None and owner[1] != member.source_hash:
                    raise ValueError(
                        f"Runtime plugins {owner[0]} and {item.plugin_id} provide conflicting "
                        f"member {member.root}:{member.path}"
                    )
                member_owners[identity] = (item.plugin_id, member.source_hash)
        return tuple(values)

    @staticmethod
    def _validate_hot_resource_member(member: RuntimeResourceFile) -> None:
        parts = tuple(Path(*member.path.split("/")).parts)
        if member.root == "agent":
            if parts[:2] != (".yy", "agents"):
                raise ValueError(
                    f"Agent-home Runtime resource is outside .yy/agents: {member.path}"
                )
            return
        allowed = bool(parts) and parts[0] in {
            "tools", "skills", "extension", "observer_plugins", "runtime-plugins",
            "runtime-resources",
        }
        harness_runtime = parts[:2] == ("harness-evolution", "runtime")
        if not (allowed or harness_runtime):
            raise ValueError(
                f"Stable Core cannot be published by Runtime reload: {member.path}"
            )

    def plan_reload(self) -> RuntimeReloadPlan:
        descriptors = self.discover()
        semantic_payload = [
            {
                "plugin_id": item.plugin_id,
                "semantic_hash": item.semantic_hash,
                "core_api_version": item.core_api_version,
                "requires_plugins": item.requires_plugins,
                "conflicts_with": item.conflicts_with,
                "contributions": [value.model_dump(mode="json") for value in item.contributions],
            }
            for item in descriptors
        ]
        semantic_hash = _sha256(_canonical_json(semantic_payload))
        source_hash = _sha256(_canonical_json([
            (item.plugin_id, item.source_hash) for item in descriptors
        ]))
        current = self.state_controller.active_runtime_generation()
        current_generation = (
            RuntimeResourceGeneration.model_validate_json(current["generation_json"], strict=True)
            if current else None
        )
        old = {item.plugin_id: item.semantic_hash for item in (current_generation.descriptors if current_generation else ())}
        new = {item.plugin_id: item.semantic_hash for item in descriptors}
        changed = tuple(sorted(name for name, digest in new.items() if old.get(name) != digest))
        removed = tuple(sorted(set(old) - set(new)))
        semantically_unchanged = current_generation is not None and not changed and not removed
        generation_id = (
            current_generation.generation_id
            if semantically_unchanged
            else _sha256(_canonical_json({
                "core_api_version": self.CORE_API_VERSION,
                "parent_generation_id": (
                    current_generation.generation_id if current_generation else None
                ),
                "semantic": semantic_payload,
            }))
        )
        artifact_root = self._artifact_root(generation_id)
        generation = RuntimeResourceGeneration(
            generation_id=generation_id,
            parent_generation_id=current_generation.generation_id if current_generation else None,
            descriptors=descriptors,
            semantic_hash=semantic_hash,
            source_hash=source_hash,
            artifact_root=artifact_root,
        )
        reasons = self._approval_reasons(current_generation, generation)
        plan_payload = {
            "generation_id": generation_id,
            "parent_generation_id": generation.parent_generation_id,
            "changed_plugins": changed,
            "removed_plugins": removed,
            "approval_required": bool(reasons),
            "reasons": reasons,
        }
        return RuntimeReloadPlan(
            plan_hash=_sha256(_canonical_json(plan_payload)),
            generation=generation,
            changed_plugins=changed,
            removed_plugins=removed,
            approval_required=bool(reasons),
            reasons=reasons,
        )

    @staticmethod
    def _approval_reasons(
        current: RuntimeResourceGeneration | None,
        candidate: RuntimeResourceGeneration,
    ) -> tuple[str, ...]:
        if current is None:
            return ()
        old = {item.plugin_id: item for item in current.descriptors}
        reasons: list[str] = []
        for item in candidate.descriptors:
            previous = old.get(item.plugin_id)
            current_profiles = {
                profile
                for contribution in item.contributions
                for profile in contribution.profiles
            }
            previous_profiles = {
                profile
                for contribution in previous.contributions
                for profile in contribution.profiles
            } if previous is not None else set()
            expanded_profiles = current_profiles - previous_profiles
            if previous is not None and expanded_profiles:
                reasons.append(
                    f"expanded profile exposure: {item.plugin_id}:"
                    f"{','.join(sorted(profile.value for profile in expanded_profiles))}"
                )
            executable = any(
                contribution.kind in {
                    RuntimeContributionKind.TOOL,
                    RuntimeContributionKind.EXTENSION,
                    RuntimeContributionKind.HOOK,
                    RuntimeContributionKind.OBSERVER,
                }
                for contribution in item.contributions
            )
            if executable and previous is None:
                reasons.append(f"new executable plugin: {item.plugin_id}")
            elif executable and previous.semantic_hash != item.semantic_hash:
                reasons.append(f"executable contract or implementation changed: {item.plugin_id}")
        for removed in sorted(set(old) - {item.plugin_id for item in candidate.descriptors}):
            if any(value.kind is RuntimeContributionKind.TOOL for value in old[removed].contributions):
                reasons.append(f"removed tool plugin: {removed}")
        return tuple(reasons)

    def acquire_reference(
        self, generation_id: str, *, owner_kind: str, owner_id: str,
    ) -> str:
        """Durably protect a Generation while an owner can still resume it."""
        self.generation(generation_id)
        return str(self.state_controller.acquire_runtime_generation_reference(
            generation_id=generation_id, owner_kind=owner_kind, owner_id=owner_id,
        )["reference_id"])

    def release_reference(self, *, owner_kind: str, owner_id: str) -> None:
        self.state_controller.release_runtime_generation_reference(
            owner_kind=owner_kind, owner_id=owner_id,
        )

    def referenced_generation(
        self, *, owner_kind: str, owner_id: str,
    ) -> str | None:
        row = self.state_controller.runtime_generation_reference(
            owner_kind=owner_kind, owner_id=owner_id,
        )
        return str(row["generation_id"]) if row is not None else None

    def report_failure(
        self,
        *,
        generation_id: str,
        plugin_id: str,
        profile: RuntimeProfile | str,
        failure_kind: RuntimePluginFailureKind | str,
        error: BaseException | str,
        actor: str = "runtime",
        threshold: int = 3,
    ) -> RuntimeReloadResult | None:
        """Record attribution and publish a safe rollback after quarantine.

        A rollback only changes the active head for later Turns.  The caller's
        immutable Snapshot and any completed side effects are never replayed.
        """
        kind = (
            failure_kind
            if isinstance(failure_kind, RuntimePluginFailureKind)
            else RuntimePluginFailureKind(failure_kind)
        )
        selected = profile if isinstance(profile, RuntimeProfile) else RuntimeProfile(profile)
        state = self.state_controller.record_runtime_plugin_failure(
            generation_id=generation_id,
            plugin_id=plugin_id,
            profile=selected.value,
            failure_kind=kind.value,
            error=error,
            counts_toward_quarantine=kind.counts_toward_quarantine,
            threshold=threshold,
        )
        if not bool(state.get("quarantined_now")):
            return None
        fallback = self.state_controller.last_healthy_runtime_plugin_generation(
            plugin_id=plugin_id, excluding_generation_id=generation_id,
        )
        if fallback is None:
            return None
        result = self.rollback_member(
            plugin_id,
            from_generation_id=str(fallback),
            actor=f"{actor}:automatic-quarantine",
        )
        self.state_controller.mark_runtime_generation_quarantined(generation_id)
        return result

    def report_success(
        self,
        *,
        generation_id: str,
        plugin_id: str,
        profile: RuntimeProfile | str,
    ) -> None:
        """Reset only the consecutive implementation-failure streak."""
        selected = profile if isinstance(profile, RuntimeProfile) else RuntimeProfile(profile)
        self.state_controller.record_runtime_plugin_success(
            generation_id=generation_id,
            plugin_id=plugin_id,
            profile=selected.value,
        )

    def collect_garbage(self, *, retention_days: int) -> tuple[str, ...]:
        """Delete only retired, unreferenced and sufficiently old artifacts."""
        removed: list[str] = []
        # A crash may occur after SQLite fenced a Generation but before its
        # directory was removed.  The marker is an intent as well as evidence;
        # finish that exact cleanup without reconsidering another Generation.
        for row in self.state_controller.runtime_generation_pending_artifact_cleanup():
            generation_id = str(row["generation_id"])
            artifact = self._artifact_root(generation_id)
            if artifact.is_dir():
                shutil.rmtree(artifact)
            removed.append(generation_id)
        candidates = self.state_controller.runtime_generation_gc_candidates(
            retention_days=retention_days,
        )
        for row in candidates:
            generation_id = str(row["generation_id"])
            artifact = self._artifact_root(generation_id)
            # Recheck after filesystem work is selected; StateController CAS
            # refuses deletion if a Run/Runtime/Harness/Recovery acquired it.
            if not self.state_controller.delete_runtime_generation_if_unreferenced(
                generation_id,
            ):
                continue
            if artifact.is_dir():
                shutil.rmtree(artifact)
            removed.append(generation_id)
        return tuple(removed)

    def reload(self, *, actor: str, approved_plan_hash: str | None = None) -> RuntimeReloadResult:
        try:
            plan = self.plan_reload()
            current = self.state_controller.active_runtime_generation()
            if current and str(current["generation_id"]) == plan.generation.generation_id:
                self.state_controller.record_runtime_reload_attempt(
                    plan, actor=actor, status="unchanged", error=None,
                )
                return RuntimeReloadResult(
                    status="unchanged", message="Runtime resources are semantically unchanged",
                    generation_id=plan.generation.generation_id,
                    plan_hash=plan.plan_hash,
                )
            if plan.approval_required and approved_plan_hash == plan.plan_hash:
                self.state_controller.approve_runtime_reload_plan(plan, actor=actor)
            approved = (
                not plan.approval_required
                or self.state_controller.runtime_reload_plan_is_approved(
                    plan_hash=plan.plan_hash,
                    generation_id=plan.generation.generation_id,
                )
            )
            if not approved:
                self.state_controller.record_runtime_reload_attempt(
                    plan, actor=actor, status="awaiting_approval", error=None,
                )
                return RuntimeReloadResult(
                    status="awaiting_approval",
                    message="Runtime resource changes require an approved reload plan",
                    generation_id=plan.generation.generation_id,
                    plan_hash=plan.plan_hash,
                    changed_plugins=plan.changed_plugins,
                )
            self._materialize(plan.generation)
            self._smoke_test(plan.generation)
            self.state_controller.activate_runtime_generation(plan, actor=actor)
            return RuntimeReloadResult(
                status="activated", message="Runtime resource generation activated",
                generation_id=plan.generation.generation_id,
                plan_hash=plan.plan_hash,
                changed_plugins=plan.changed_plugins,
            )
        except Exception as exc:
            try:
                self.state_controller.record_runtime_reload_failure(actor=actor, error=exc)
            except Exception:
                pass
            raise

    def ensure_initial_generation(self) -> RuntimeResourceGeneration:
        active = self.state_controller.active_runtime_generation()
        if active:
            generation = RuntimeResourceGeneration.model_validate_json(
                active["generation_json"], strict=True,
            )
            self._materialize(generation)
            self._smoke_test(generation)
            return generation
        result = self.reload(actor="gateway:bootstrap")
        if result.status != "activated" or not result.generation_id:
            raise RuntimeError("Initial Runtime resource generation was not activated")
        return self.generation(result.generation_id)

    def generation(self, generation_id: str) -> RuntimeResourceGeneration:
        row = self.state_controller.runtime_generation(generation_id)
        if row is None:
            raise KeyError(generation_id)
        return RuntimeResourceGeneration.model_validate_json(row["generation_json"], strict=True)

    def snapshot(
        self, profile: RuntimeProfile | str, *, generation_id: str | None = None,
    ) -> RuntimeResourceSnapshot:
        selected = profile if isinstance(profile, RuntimeProfile) else RuntimeProfile(profile)
        row = (
            self.state_controller.runtime_generation(generation_id)
            if generation_id else self.state_controller.active_runtime_generation()
        )
        if row is None:
            raise RuntimeError("No active Runtime resource generation")
        generation = RuntimeResourceGeneration.model_validate_json(row["generation_json"], strict=True)
        selected_descriptors = tuple(
            item for item in generation.descriptors
            if any(selected in contribution.profiles for contribution in item.contributions)
        )
        kinds: dict[RuntimeContributionKind, list[str]] = {kind: [] for kind in RuntimeContributionKind}
        for descriptor in selected_descriptors:
            for contribution in descriptor.contributions:
                if selected in contribution.profiles:
                    kinds[contribution.kind].append(
                        _canonical_json({
                            "plugin": descriptor.plugin_id,
                            "semantic": descriptor.semantic_hash,
                            "contract": contribution.contract,
                        })
                    )
        artifact = self._materialize_profile_view(generation, selected, selected_descriptors)
        return RuntimeResourceSnapshot(
            generation_id=generation.generation_id,
            profile=selected,
            descriptor_ids=tuple(item.plugin_id for item in selected_descriptors),
            contributions=tuple(
                ResolvedRuntimeContribution(
                    plugin_id=descriptor.plugin_id,
                    plugin_version=descriptor.plugin_version,
                    kind=contribution.kind,
                    name=contribution.name,
                    source_hash=descriptor.source_hash,
                    semantic_hash=descriptor.semantic_hash,
                    contract=contribution.contract,
                )
                for descriptor in selected_descriptors
                for contribution in descriptor.contributions
                if selected in contribution.profiles
            ),
            source_root=artifact / "source",
            agent_root=artifact / "agent",
            tool_catalog_hash=_sha256("\n".join(sorted(kinds[RuntimeContributionKind.TOOL]))),
            skill_catalog_hash=_sha256("\n".join(sorted(kinds[RuntimeContributionKind.SKILL]))),
            prompt_hash=_sha256("\n".join(sorted(kinds[RuntimeContributionKind.STABLE_PROMPT]))),
            hook_plan_hash=_sha256("\n".join(sorted(
                kinds[RuntimeContributionKind.HOOK]
                + kinds[RuntimeContributionKind.EXTENSION]
                + kinds[RuntimeContributionKind.DYNAMIC_CONTEXT]
                + kinds[RuntimeContributionKind.OBSERVER]
            ))),
        )

    def rollback_member(
        self, plugin_id: str, *, from_generation_id: str, actor: str,
    ) -> RuntimeReloadResult:
        current_row = self.state_controller.active_runtime_generation()
        if current_row is None:
            raise RuntimeError("No active generation to roll back")
        current = RuntimeResourceGeneration.model_validate_json(
            current_row["generation_json"], strict=True,
        )
        previous = self.generation(from_generation_id)
        replacement = next((item for item in previous.descriptors if item.plugin_id == plugin_id), None)
        if replacement is None:
            raise KeyError(plugin_id)
        descriptors = tuple(sorted(
            [item for item in current.descriptors if item.plugin_id != plugin_id] + [replacement],
            key=lambda item: item.plugin_id,
        ))
        semantic_hash = _sha256(_canonical_json([
            (item.plugin_id, item.semantic_hash) for item in descriptors
        ]))
        generation_id = _sha256(_canonical_json({
            "core_api_version": self.CORE_API_VERSION,
            "parent_generation_id": current.generation_id,
            "rollback_plugin": plugin_id,
            "rollback_source_generation": from_generation_id,
            "members": [(item.plugin_id, item.semantic_hash) for item in descriptors],
        }))
        generation = RuntimeResourceGeneration(
            generation_id=generation_id,
            parent_generation_id=current.generation_id,
            descriptors=descriptors,
            semantic_hash=semantic_hash,
            source_hash=_sha256(_canonical_json([
                (item.plugin_id, item.source_hash) for item in descriptors
            ])),
            artifact_root=self._artifact_root(generation_id),
        )
        self._materialize(generation, fallback_generations=(current, previous))
        self._smoke_test(generation)
        payload = {
            "generation_id": generation_id,
            "parent_generation_id": current.generation_id,
            "changed_plugins": [plugin_id],
            "removed_plugins": [],
            "approval_required": False,
            "reasons": [f"rollback:{plugin_id}:{from_generation_id}"],
        }
        plan = RuntimeReloadPlan(
            plan_hash=_sha256(_canonical_json(payload)), generation=generation,
            changed_plugins=(plugin_id,), reasons=(payload["reasons"][0],),
        )
        self.state_controller.activate_runtime_generation(plan, actor=actor)
        return RuntimeReloadResult(
            status="activated", message=f"Rolled back {plugin_id} in a new generation",
            generation_id=generation_id, plan_hash=plan.plan_hash,
            changed_plugins=(plugin_id,),
        )

    def _materialize(
        self,
        generation: RuntimeResourceGeneration,
        *,
        fallback_generations: tuple[RuntimeResourceGeneration, ...] = (),
    ) -> None:
        target = generation.artifact_root
        if target.is_dir():
            return
        self.storage_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(mkdtemp(prefix=f".{generation.generation_id[:8]}.", dir=self.storage_root))
        try:
            for descriptor in generation.descriptors:
                for member in descriptor.files:
                    destination = temporary / member.root / Path(*member.path.split("/"))
                    source_base = self.source_root if member.root == "source" else self.agent_root
                    source = source_base / Path(*member.path.split("/"))
                    if not source.is_file() or _sha256(source.read_bytes()) != member.source_hash:
                        source = self._member_from_fallback(member, fallback_generations)
                    if source is None or not source.is_file():
                        raise RuntimeError(f"Runtime generation member is unavailable: {member.path}")
                    raw = source.read_bytes()
                    if _sha256(raw) != member.source_hash:
                        raise RuntimeError(f"Runtime generation member hash mismatch: {member.path}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
            manifest = temporary / "generation.json"
            with manifest.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(generation.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temporary, target)
            except OSError:
                if not target.is_dir():
                    raise
                shutil.rmtree(temporary, ignore_errors=True)
            try:
                directory_fd = os.open(str(self.storage_root), os.O_RDONLY)
            except (AttributeError, OSError):
                pass
            else:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _member_from_fallback(
        member: RuntimeResourceFile,
        generations: tuple[RuntimeResourceGeneration, ...],
    ) -> Path | None:
        for generation in generations:
            candidate = generation.artifact_root / member.root / Path(*member.path.split("/"))
            if candidate.is_file() and _sha256(candidate.read_bytes()) == member.source_hash:
                return candidate
        return None

    def _materialize_profile_view(
        self,
        generation: RuntimeResourceGeneration,
        profile: RuntimeProfile,
        descriptors: tuple[RuntimePluginDescriptor, ...],
    ) -> Path:
        """Create a derived file view containing only profile-authorized members."""
        profile_name = profile.value.replace(":", "-")
        target = generation.artifact_root / "profiles" / profile_name
        expected = {
            (member.root, self._profile_member_path(profile, member)): (
                member.path, member.source_hash
            )
            for descriptor in descriptors
            for member in descriptor.files
        }
        if target.is_dir():
            for (root, path), (_, digest) in expected.items():
                candidate = target / root / Path(*path.split("/"))
                if not candidate.is_file() or _sha256(candidate.read_bytes()) != digest:
                    raise RuntimeError(
                        f"Runtime profile Snapshot is damaged: {profile.value}:{path}"
                    )
            return target
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(mkdtemp(prefix=f".{profile_name[:8]}.", dir=parent))
        try:
            for (root, path), (original_path, digest) in sorted(expected.items()):
                source = generation.artifact_root / root / Path(*original_path.split("/"))
                raw = source.read_bytes()
                if _sha256(raw) != digest:
                    raise RuntimeError(f"Runtime profile source changed: {path}")
                destination = temporary / root / Path(*path.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            try:
                os.replace(temporary, target)
            except OSError:
                if not target.is_dir():
                    raise
                shutil.rmtree(temporary, ignore_errors=True)
                for (root, path), (_, digest) in expected.items():
                    candidate = target / root / Path(*path.split("/"))
                    if not candidate.is_file() or _sha256(candidate.read_bytes()) != digest:
                        raise RuntimeError(
                            f"Concurrent Runtime profile publication conflicted: "
                            f"{profile.value}:{path}"
                        )
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target

    @staticmethod
    def _profile_member_path(
        profile: RuntimeProfile, member: RuntimeResourceFile,
    ) -> str:
        if profile.value.startswith("harness:"):
            prefix = "harness-evolution/runtime/"
            if member.root == "source" and member.path.startswith(prefix):
                return member.path[len(prefix):]
        if profile in {
            RuntimeProfile.INTERACTIVE, RuntimeProfile.CRON, RuntimeProfile.DREAM,
        }:
            prefix = f"runtime-resources/{profile.value}/"
            if member.root == "source" and member.path.startswith(prefix):
                return member.path[len(prefix):]
        return member.path

    @staticmethod
    def _smoke_test(generation: RuntimeResourceGeneration) -> None:
        manifest = generation.artifact_root / "generation.json"
        if not manifest.is_file():
            raise RuntimeError("Runtime generation manifest is missing")
        persisted = RuntimeResourceGeneration.model_validate_json(
            manifest.read_text(encoding="utf-8"), strict=True,
        )
        if persisted.generation_id != generation.generation_id:
            raise RuntimeError("Runtime generation manifest identity mismatch")
        for descriptor in generation.descriptors:
            for member in descriptor.files:
                path = generation.artifact_root / member.root / Path(*member.path.split("/"))
                if not path.is_file() or _sha256(path.read_bytes()) != member.source_hash:
                    raise RuntimeError(f"Runtime generation verification failed: {member.path}")
        contribution_kinds = {
            contribution.kind
            for descriptor in generation.descriptors
            for contribution in descriptor.contributions
        }
        if RuntimeContributionKind.TOOL in contribution_kinds:
            digest = "0" * 64
            snapshot = RuntimeResourceSnapshot(
                generation_id=generation.generation_id,
                profile=RuntimeProfile.INTERACTIVE,
                descriptor_ids=tuple(item.plugin_id for item in generation.descriptors),
                source_root=generation.artifact_root / "source",
                agent_root=generation.artifact_root / "agent",
                tool_catalog_hash=digest,
                skill_catalog_hash=digest,
                prompt_hash=digest,
                hook_plan_hash=digest,
            )
            load_generation_tool_module(snapshot)
        if RuntimeContributionKind.SKILL in contribution_kinds:
            from skill.parser import parse_skill

            for descriptor in generation.descriptors:
                if not any(
                    item.kind is RuntimeContributionKind.SKILL
                    for item in descriptor.contributions
                ):
                    continue
                for member in descriptor.files:
                    if Path(member.path).name == "SKILL.md":
                        skill_file = (
                            generation.artifact_root
                            / member.root
                            / Path(*member.path.split("/"))
                        )
                        parse_skill(skill_file.parent)
        if RuntimeContributionKind.EXTENSION in contribution_kinds:
            from Agent.extensions import ExtensionLoader

            catalog = ExtensionLoader(generation.artifact_root / "source").scan()
            if catalog.rejections:
                first = catalog.rejections[0]
                raise RuntimeError(
                    f"Extension smoke validation rejected {first.get('path', 'resource')}: "
                    f"{first.get('reason', 'invalid extension')}"
                )


def load_generation_tool_module(snapshot: RuntimeResourceSnapshot):
    """Load the snapshotted ``tools`` package under a generation-specific name."""
    init = snapshot.source_root / "tools" / "__init__.py"
    if not init.is_file():
        return None
    module_name = f"yy_runtime_tools_{snapshot.generation_id[:24]}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        module_name, init, submodule_search_locations=[str(init.parent)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load snapshotted Tool package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


class RuntimePluginWatcher:
    """Polling watcher which only proposes/activates stable, validated trees."""

    def __init__(
        self,
        manager: RuntimePluginManager,
        *,
        poll_seconds: float = 1.0,
        debounce_seconds: float = 1.5,
        write_gate: Any | None = None,
    ) -> None:
        self.manager = manager
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.debounce_seconds = max(self.poll_seconds, float(debounce_seconds))
        self.write_gate = write_gate
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._last_source_hash: str | None = None
        self._stable_since = 0.0
        self._handled_plan_hash: str | None = None
        self.last_error: str | None = None
        self._maintenance_epoch: int | None = None
        self._build_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._run(), name="runtime-plugin-watcher")

    async def close(self) -> None:
        self._closing = True
        # ``asyncio.to_thread`` cannot be force-cancelled.  Wait for any
        # already-started discovery/activation transaction to reach its
        # atomic boundary before cancelling the polling coroutine.
        async with self._build_lock:
            pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def quiesce(self, maintenance_epoch: int):
        """Fence discovery/activation and wait for an in-flight build boundary."""
        from backup import QuiesceResult

        self._maintenance_epoch = maintenance_epoch
        async with self._build_lock:
            pass
        return QuiesceResult(
            participant="runtime_plugins",
            maintenance_epoch=maintenance_epoch,
            acknowledged=True,
            safe_boundary="no_reload_build_or_activation",
        )

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None
            self._stable_since = 0.0

    def status(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "last_source_hash": self._last_source_hash,
            "handled_plan_hash": self._handled_plan_hash,
            "last_error": self.last_error,
            "maintenance_epoch": self._maintenance_epoch,
        }

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closing:
            await asyncio.sleep(self.poll_seconds)
            if self._maintenance_epoch is not None:
                self._stable_since = 0.0
                continue
            gate_state = getattr(self.write_gate, "state", None)
            if gate_state is not None and str(getattr(gate_state, "value", gate_state)) != "running":
                self._stable_since = 0.0
                continue
            try:
                async with self._build_lock:
                    if self._maintenance_epoch is not None or self._closing:
                        continue
                    plan = await asyncio.to_thread(self.manager.plan_reload)
                source_hash = plan.generation.source_hash
                now = loop.time()
                if source_hash != self._last_source_hash:
                    self._last_source_hash = source_hash
                    self._stable_since = now
                    continue
                if now - self._stable_since < self.debounce_seconds:
                    continue
                active = self.manager.state_controller.active_runtime_generation()
                if active and str(active["generation_id"]) == plan.generation.generation_id:
                    self._handled_plan_hash = plan.plan_hash
                    continue
                if plan.plan_hash == self._handled_plan_hash:
                    continue
                async with self._build_lock:
                    if self._maintenance_epoch is not None or self._closing:
                        continue
                    result = await asyncio.to_thread(
                        self.manager.reload, actor="gateway:file-watcher",
                    )
                self._handled_plan_hash = result.plan_hash
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"



__all__ = [
    "FileTreeRuntimeResourceProvider",
    "RuntimeContributionKind",
    "RuntimePluginContribution",
    "RuntimePluginDescriptor",
    "RuntimePluginFailureKind",
    "RuntimePluginManager",
    "RuntimePluginWatcher",
    "RuntimeProfile",
    "ResolvedRuntimeContribution",
    "RuntimeReloadPlan",
    "RuntimeReloadResult",
    "RuntimeResourceBundle",
    "RuntimeResourceFile",
    "RuntimeResourceGeneration",
    "RuntimeResourceProvider",
    "StaticRuntimeResourceProvider",
    "RuntimeResourceSnapshot",
    "default_runtime_resource_providers",
    "load_generation_tool_module",
    "register_runtime_resource_callbacks",
]
