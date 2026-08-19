"""In-process Hook extensions with versioned capabilities and failure isolation.

This is a controlled execution boundary, not a security sandbox for malicious
Python.  The loader performs a conservative AST scan before importing code and
the runtime exposes capability-checked facades instead of raw services.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from Agent.hook import (
    HookEvent,
    HookFailureMode,
    HookOrigin,
    HookPoint,
    HookRegistry,
)


_FILE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}\.py$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DENIED_IMPORTS = {
    "builtins", "ctypes", "httpx", "importlib", "inspect", "io", "marshal",
    "pickle", "requests", "socket", "sqlite3", "subprocess", "sys", "urllib",
    "os", "pathlib", "shutil", "gateway", "memory.store", "tool.registry",
    "Agent.runtime",
}
_DENIED_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "getattr", "setattr",
    "delattr", "vars", "globals", "locals", "object.__getattribute__",
}
_DENIED_ATTRIBUTES = {
    "os.system", "os.popen", "os.getenv", "os.remove", "os.unlink", "os.rename",
    "os.replace", "os.mkdir", "os.makedirs", "os.rmdir", "os.fork",
    "shutil.copy", "shutil.copy2", "shutil.copytree", "shutil.move", "shutil.rmtree",
    "Path.write_text", "Path.write_bytes", "Path.unlink", "Path.rename", "Path.replace",
    "Path.mkdir", "Path.rmdir", "Path.chmod", "Path.touch", "Path.read_text", "Path.read_bytes",
}


class CapabilityTier(str, Enum):
    SAFE = "safe"
    CONTROLLED = "controlled"
    PRIVILEGED = "privileged"


class ExtensionCapability(str, Enum):
    SESSION_READ = "session.read"
    STATE_READ = "state.read"
    LOGGER_WRITE = "logger.write"
    WORKSPACE_READ = "workspace.read"
    MEMORY_READ = "memory.read"
    MEMORY_APPEND = "memory.append"
    MODEL_REQUEST_MODIFY = "model.request.modify"
    TOOL_REQUEST_MODIFY = "tool.request.modify"
    TOOL_INVOKE = "tool.invoke"

    @property
    def tier(self) -> CapabilityTier:
        if self in {
            self.SESSION_READ, self.STATE_READ, self.LOGGER_WRITE, self.WORKSPACE_READ,
        }:
            return CapabilityTier.SAFE
        if self is self.TOOL_INVOKE:
            return CapabilityTier.PRIVILEGED
        return CapabilityTier.CONTROLLED


class ExtensionManifest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    capabilities: tuple[ExtensionCapability, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    @field_validator("capabilities", mode="before")
    @classmethod
    def parse_capabilities(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return tuple(
                item if isinstance(item, ExtensionCapability) else ExtensionCapability(item)
                for item in value
            )
        return value

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def freeze_tools(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_tools(self) -> "ExtensionManifest":
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Extension capabilities must be unique")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("Extension allowed_tools must be unique")
        if any(not _TOOL_NAME.fullmatch(name) or "*" in name for name in self.allowed_tools):
            raise ValueError("Extension allowed_tools must contain exact Tool names")
        if self.allowed_tools and ExtensionCapability.TOOL_INVOKE not in self.capabilities:
            raise ValueError("allowed_tools requires tool.invoke")
        if ExtensionCapability.TOOL_INVOKE in self.capabilities and not self.allowed_tools:
            raise ValueError("tool.invoke requires at least one exact allowed Tool")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )


class ExtensionTraceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    trace_id: str
    hook_id: str
    stage: HookPoint
    source_hash: str
    manifest_hash: str
    grant_version: int = Field(ge=0)
    requested_capabilities: tuple[ExtensionCapability, ...] = ()
    requested_allowed_tools: tuple[str, ...] = ()
    effective_capabilities: tuple[ExtensionCapability, ...] = ()
    effective_allowed_tools: tuple[str, ...] = ()
    tool_contract_hashes: dict[str, str] = Field(default_factory=dict)

    def permits(self, capability: ExtensionCapability) -> bool:
        return capability in self.effective_capabilities


class ExtensionRuntimePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    allowed_capabilities: tuple[ExtensionCapability, ...] = tuple(ExtensionCapability)
    allowed_tools: tuple[str, ...] = ()


class ExtensionCapabilityDenied(PermissionError):
    pass


class ExtensionContractViolation(RuntimeError):
    pass


class ExtensionQuarantined(RuntimeError):
    pass


class ExtensionEventView:
    """Read-only snapshot. Mutations must use ExtensionContext methods."""

    __slots__ = ("point", "session_id", "data")

    def __init__(self, event: HookEvent) -> None:
        self.point = event.point
        self.session_id = event.session_id
        self.data = MappingProxyType(copy.deepcopy(event.data))


class ExtensionMutationBuffer:
    __slots__ = ("_patch",)

    def __init__(self) -> None:
        self._patch: dict[str, Any] = {}

    def replace(self, field: str, value: Any) -> None:
        self._patch[field] = copy.deepcopy(value)

    def commit(
        self, event: HookEvent, *, allowed_tool_names: set[str] | None = None,
    ) -> None:
        if not self._patch:
            return
        allowed: set[str]
        if event.point is HookPoint.MODEL_BEFORE:
            allowed = {"messages", "tools"}
            if "messages" in self._patch and not isinstance(self._patch["messages"], list):
                raise ExtensionContractViolation("model messages patch must be a list")
            if "tools" in self._patch and not isinstance(self._patch["tools"], list):
                raise ExtensionContractViolation("model tools patch must be a list")
            if "messages" in self._patch and any(
                not isinstance(message, dict) or not isinstance(message.get("role"), str)
                for message in self._patch["messages"]
            ):
                raise ExtensionContractViolation("model messages patch contains an invalid message")
            if "tools" in self._patch:
                tool_names: list[str] = []
                for schema in self._patch["tools"]:
                    if not isinstance(schema, dict):
                        raise ExtensionContractViolation("model tools patch contains a non-object")
                    name = schema.get("name")
                    if (
                        not isinstance(name, str) or not _TOOL_NAME.fullmatch(name)
                        or not isinstance(schema.get("description"), str)
                        or not isinstance(schema.get("parameters"), dict)
                    ):
                        raise ExtensionContractViolation("model tools patch contains an invalid Schema")
                    tool_names.append(name)
                if len(tool_names) != len(set(tool_names)):
                    raise ExtensionContractViolation("model tools patch contains duplicate names")
                if allowed_tool_names is not None and not set(tool_names).issubset(allowed_tool_names):
                    raise ExtensionContractViolation(
                        "model tools patch cannot expose an unregistered Tool"
                    )
        elif event.point is HookPoint.TOOL_BEFORE:
            allowed = {"name", "arguments"}
            if "name" in self._patch and not isinstance(self._patch["name"], str):
                raise ExtensionContractViolation("Tool name patch must be a string")
            if "arguments" in self._patch and not isinstance(self._patch["arguments"], dict):
                raise ExtensionContractViolation("Tool arguments patch must be an object")
        else:
            raise ExtensionContractViolation(f"{event.point.value} does not allow request patches")
        unexpected = set(self._patch) - allowed
        if unexpected:
            raise ExtensionContractViolation(f"Unsupported patch fields: {sorted(unexpected)}")
        candidate = copy.deepcopy(event.data)
        candidate.update(copy.deepcopy(self._patch))
        event.data = candidate


@dataclass
class ExtensionRuntimeBinding:
    run_id: str | None = None
    trace_id: str = "unbound"


@dataclass
class ExtensionServices:
    workspace_root: Path
    memory: Any | None = None
    state_backend: Any | None = None
    tool_registry: Any | None = None
    tool_context: Any | None = None


class ExtensionContext:
    """Capability checked facade passed to an Extension Hook."""

    __slots__ = (
        "provider", "model", "sandbox_enabled", "snapshot", "event_point", "session_id",
        "_buffer", "_services", "_binding", "_invocation_id", "_tool_ordinal",
    )

    def __init__(
        self, *, provider: str, model: str, sandbox_enabled: bool,
        snapshot: ExtensionTraceSnapshot, event_point: HookPoint, session_id: str,
        mutation_buffer: ExtensionMutationBuffer, services: ExtensionServices,
        binding: ExtensionRuntimeBinding, invocation_id: str,
    ) -> None:
        self.provider = provider
        self.model = model
        self.sandbox_enabled = sandbox_enabled
        self.snapshot = snapshot
        self.event_point = event_point
        self.session_id = session_id
        self._buffer = mutation_buffer
        self._services = services
        self._binding = binding
        self._invocation_id = invocation_id
        self._tool_ordinal = 0

    def _require(self, capability: ExtensionCapability) -> None:
        if not self.snapshot.permits(capability):
            raise ExtensionCapabilityDenied(f"Extension capability denied: {capability.value}")

    def replace_model_messages(self, messages: list[dict[str, Any]]) -> None:
        self._require(ExtensionCapability.MODEL_REQUEST_MODIFY)
        if self.event_point is not HookPoint.MODEL_BEFORE:
            raise ExtensionContractViolation("model.request.modify is only valid at model_before")
        self._buffer.replace("messages", messages)

    def replace_model_tools(self, tools: list[dict[str, Any]]) -> None:
        self._require(ExtensionCapability.MODEL_REQUEST_MODIFY)
        if self.event_point is not HookPoint.MODEL_BEFORE:
            raise ExtensionContractViolation("model.request.modify is only valid at model_before")
        self._buffer.replace("tools", tools)

    def replace_tool_arguments(self, arguments: dict[str, Any]) -> None:
        self._require(ExtensionCapability.TOOL_REQUEST_MODIFY)
        if self.event_point is not HookPoint.TOOL_BEFORE:
            raise ExtensionContractViolation("tool.request.modify is only valid at tool_before")
        self._buffer.replace("arguments", arguments)

    def replace_tool_name(self, name: str) -> None:
        self._require(ExtensionCapability.TOOL_REQUEST_MODIFY)
        if self.event_point is not HookPoint.TOOL_BEFORE:
            raise ExtensionContractViolation("tool.request.modify is only valid at tool_before")
        self._buffer.replace("name", name)

    def read_workspace(self, relative_path: str, *, max_bytes: int = 1_048_576) -> str:
        self._require(ExtensionCapability.WORKSPACE_READ)
        candidate = (self._services.workspace_root / relative_path).resolve()
        root = self._services.workspace_root.resolve()
        if candidate == root or root not in candidate.parents or candidate.is_symlink():
            raise ExtensionContractViolation("workspace.read path escapes the workspace")
        if not candidate.is_file() or candidate.stat().st_size > max_bytes:
            raise ExtensionContractViolation("workspace.read requires a bounded regular file")
        return candidate.read_text(encoding="utf-8")

    def read_session(self) -> tuple[dict[str, Any], ...]:
        self._require(ExtensionCapability.SESSION_READ)
        memory = self._services.memory
        if memory is None or not hasattr(memory, "session_records"):
            raise ExtensionCapabilityDenied("session.read is unavailable in this Runtime")
        return tuple(dict(record) for record in memory.session_records(self.session_id))

    def read_state(self) -> dict[str, Any]:
        self._require(ExtensionCapability.STATE_READ)
        backend = self._services.state_backend
        if backend is None or self._binding.run_id is None:
            raise ExtensionCapabilityDenied("state.read requires a Gateway Run binding")
        return backend.state(self._binding.run_id).model_dump(mode="json")

    def read_memory(self) -> str:
        self._require(ExtensionCapability.MEMORY_READ)
        memory = self._services.memory
        if memory is None or not hasattr(memory, "profile_context"):
            raise ExtensionCapabilityDenied("memory.read is unavailable in this Runtime")
        return str(memory.profile_context(self.session_id))

    def append_memory(self, content: str) -> str:
        self._require(ExtensionCapability.MEMORY_APPEND)
        memory = self._services.memory
        if memory is None or not hasattr(memory, "record_extension_annotation"):
            raise ExtensionCapabilityDenied("memory.append is unavailable in this Runtime")
        return str(memory.record_extension_annotation(
            self.session_id, content,
            hook_id=self.snapshot.hook_id,
            source_hash=self.snapshot.source_hash,
            run_id=self._binding.run_id,
        ))

    def log(self, message: str, **fields: Any) -> None:
        self._require(ExtensionCapability.LOGGER_WRITE)
        backend = self._services.state_backend
        if backend is not None and hasattr(backend, "record_extension_audit"):
            backend.record_extension_audit(
                self.snapshot, decision="log", result=message,
                run_id=self._binding.run_id, details=fields,
            )

    async def invoke_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self._require(ExtensionCapability.TOOL_INVOKE)
        registry, context = self._services.tool_registry, self._services.tool_context
        if registry is None or context is None:
            raise ExtensionCapabilityDenied("tool.invoke is unavailable in this Runtime")
        preapproved = name in self.snapshot.effective_allowed_tools
        backend = self._services.state_backend
        if backend is not None and hasattr(backend, "record_extension_audit"):
            backend.record_extension_audit(
                self.snapshot,
                decision="extension_tool_authorization",
                result="allow" if preapproved else "deny",
                run_id=self._binding.run_id,
                details={"requested_tool": name, "tool_preapproved": preapproved},
            )
        self._tool_ordinal += 1
        from tool import ExtensionToolAuthorization
        authorization = ExtensionToolAuthorization(
            hook_id=self.snapshot.hook_id,
            source_hash=self.snapshot.source_hash,
            manifest_hash=self.snapshot.manifest_hash,
            grant_version=self.snapshot.grant_version,
            allowed_tools=self.snapshot.effective_allowed_tools,
            tool_contract_hashes=self.snapshot.tool_contract_hashes,
            trace_id=self.snapshot.trace_id,
        )
        extension_context = context.model_copy(update={"extension_authorization": authorization})
        call_id = hashlib.sha256(
            f"{self.snapshot.trace_id}:{self.snapshot.hook_id}:"
            f"{self._invocation_id}:{self._tool_ordinal}".encode()
        ).hexdigest()[:32]
        return await registry.execute(name, arguments, extension_context, tool_call_id=call_id)


class ExtensionModule(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    stage: HookPoint
    name: str = Field(min_length=1, max_length=128)
    priority: int = Field(ge=-50, le=50)
    path: Path
    handle: Any
    manifest: ExtensionManifest
    source_hash: str
    manifest_hash: str
    hook_id: str


class ExtensionCatalog:
    def __init__(
        self,
        modules: Mapping[HookPoint, tuple[ExtensionModule, ...]] | None = None,
        *, rejections: tuple[dict[str, str], ...] = (),
    ) -> None:
        self._modules = MappingProxyType({
            point: tuple((modules or {}).get(point, ())) for point in HookPoint
        })
        self.rejections = rejections

    @property
    def modules(self) -> Mapping[HookPoint, tuple[ExtensionModule, ...]]:
        return self._modules

    def register(
        self,
        registry: HookRegistry,
        *, provider: str, model: str, sandbox_enabled: bool,
        services: ExtensionServices,
        binding: ExtensionRuntimeBinding,
        runtime_policy: ExtensionRuntimePolicy | None = None,
    ) -> None:
        policy = runtime_policy or ExtensionRuntimePolicy()
        snapshots = {
            module.hook_id: self._snapshot(module, services, binding, policy)
            for modules in self._modules.values() for module in modules
        }
        invocation_counts = {hook_id: 0 for hook_id in snapshots}
        for point in HookPoint:
            for extension in self._modules[point]:
                snapshot = snapshots[extension.hook_id]

                async def callback(
                    event: HookEvent, *, _extension=extension, _snapshot=snapshot,
                ) -> None:
                    backend = services.state_backend
                    if backend is not None and backend.extension_hook_is_quarantined(
                        _extension.hook_id, _extension.stage.value, _extension.source_hash,
                    ):
                        raise ExtensionQuarantined(
                            f"Extension source is quarantined: {_extension.hook_id}"
                        )
                    buffer = ExtensionMutationBuffer()
                    invocation_counts[_extension.hook_id] += 1
                    invocation_id = hashlib.sha256(
                        f"{_snapshot.trace_id}:{_extension.hook_id}:"
                        f"{invocation_counts[_extension.hook_id]}".encode()
                    ).hexdigest()[:32]
                    context = ExtensionContext(
                        provider=provider, model=model, sandbox_enabled=sandbox_enabled,
                        snapshot=_snapshot, event_point=event.point, session_id=event.session_id,
                        mutation_buffer=buffer, services=services, binding=binding,
                        invocation_id=invocation_id,
                    )
                    result = _extension.handle(ExtensionEventView(event), context)
                    if inspect.isawaitable(result):
                        await result
                    allowed_tool_names = None
                    if services.tool_registry is not None:
                        allowed_tool_names = set(services.tool_registry.names(services.tool_context))
                    buffer.commit(event, allowed_tool_names=allowed_tool_names)

                async def reporter(
                    outcome: str, error: BaseException | None, duration: float,
                    *, _extension=extension, _snapshot=snapshot,
                ) -> None:
                    backend = services.state_backend
                    if backend is None:
                        return
                    if isinstance(error, ExtensionQuarantined):
                        classification = "quarantined"
                    elif (
                        isinstance(error, ExtensionCapabilityDenied)
                        or bool(getattr(error, "extension_policy_denial", False))
                    ):
                        classification = "policy_denial"
                    elif isinstance(error, ExtensionContractViolation):
                        classification = "contract_violation"
                    else:
                        classification = outcome
                    backend.record_extension_hook_outcome(
                        _snapshot, classification=classification, duration=duration,
                        error=error, run_id=binding.run_id,
                    )

                registry.register(
                    point, callback, priority=extension.priority,
                    identity=extension.hook_id, origin=HookOrigin.EXTENSION,
                    failure_mode=HookFailureMode.ISOLATE,
                    timeout_seconds=extension.manifest.timeout_seconds,
                    outcome_reporter=reporter,
                )

    @staticmethod
    def _snapshot(
        module: ExtensionModule, services: ExtensionServices,
        binding: ExtensionRuntimeBinding, policy: ExtensionRuntimePolicy,
    ) -> ExtensionTraceSnapshot:
        grant: dict[str, Any] | None = None
        backend = services.state_backend
        if backend is not None:
            grant = backend.resolve_extension_grant(
                module.hook_id, module.stage.value, module.source_hash, module.manifest_hash,
            )
        granted_capabilities = {
            ExtensionCapability(value) for value in (grant or {}).get("granted_capabilities", ())
        }
        effective_capabilities = tuple(
            capability for capability in module.manifest.capabilities
            if capability in granted_capabilities and capability in policy.allowed_capabilities
        )
        granted_tools = set((grant or {}).get("granted_tools", ()))
        policy_tools = set(policy.allowed_tools) if policy.allowed_tools else set(module.manifest.allowed_tools)
        contract_hashes = dict((grant or {}).get("tool_contract_hashes", {}))
        registry = services.tool_registry
        effective_tools: list[str] = []
        if ExtensionCapability.TOOL_INVOKE in effective_capabilities and registry is not None:
            for name in module.manifest.allowed_tools:
                if name not in granted_tools or name not in policy_tools:
                    continue
                if not registry.extension_preapproval_allowed(name):
                    continue
                current_hash = registry.tool_contract_hash(name)
                if contract_hashes.get(name) == current_hash:
                    effective_tools.append(name)
        return ExtensionTraceSnapshot(
            trace_id=binding.trace_id,
            hook_id=module.hook_id,
            stage=module.stage,
            source_hash=module.source_hash,
            manifest_hash=module.manifest_hash,
            grant_version=int((grant or {}).get("grant_version", 0)),
            requested_capabilities=module.manifest.capabilities,
            requested_allowed_tools=module.manifest.allowed_tools,
            effective_capabilities=effective_capabilities,
            effective_allowed_tools=tuple(effective_tools),
            tool_contract_hashes={name: contract_hashes[name] for name in effective_tools},
        )


class ExtensionLoader:
    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root.resolve()
        self.hook_root = (self.source_root / "extension" / "hook").resolve()

    def scan(self, *, strict: bool = False) -> ExtensionCatalog:
        found: dict[HookPoint, tuple[ExtensionModule, ...]] = {}
        rejections: list[dict[str, str]] = []
        for point in HookPoint:
            found[point] = self._scan_stage(point, strict=strict, rejections=rejections)
        return ExtensionCatalog(found, rejections=tuple(rejections))

    def _scan_stage(
        self, point: HookPoint, *, strict: bool, rejections: list[dict[str, str]],
    ) -> tuple[ExtensionModule, ...]:
        stage_dir = self.hook_root / point.value
        if not stage_dir.exists():
            return ()
        if stage_dir.is_symlink() or not stage_dir.is_dir():
            raise ValueError(f"Invalid Extension stage directory: {stage_dir}")
        stage_dir = stage_dir.resolve()
        try:
            stage_dir.relative_to(self.hook_root)
        except ValueError as exc:
            raise ValueError(f"Extension stage path escapes root: {stage_dir}") from exc
        modules: list[ExtensionModule] = []
        names: set[str] = set()
        for path in sorted(stage_dir.iterdir(), key=lambda item: item.name):
            if path.is_dir() or path.name.startswith((".", "~")) or path.suffix != ".py":
                continue
            try:
                module = self._load_module(point, path)
                if module.name in names:
                    raise ValueError(f"Duplicate Extension name: {point.value}/{module.name}")
                names.add(module.name)
                modules.append(module)
            except Exception as exc:
                if strict:
                    raise
                rejections.append({"stage": point.value, "path": str(path), "reason": str(exc)})
        return tuple(sorted(modules, key=lambda item: (item.priority, item.path.name)))

    def _load_module(self, point: HookPoint, path: Path) -> ExtensionModule:
        if path.is_symlink() or not _FILE_NAME.fullmatch(path.name):
            raise ValueError(f"Invalid Extension file: {point.value}/{path.name}")
        resolved = path.resolve()
        if resolved.parent != (self.hook_root / point.value).resolve():
            raise ValueError(f"Extension file escapes stage directory: {path}")
        source = resolved.read_bytes()
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Extension source must be UTF-8") from exc
        tree = ast.parse(text, filename=str(resolved))
        self._security_scan(tree)
        constants = self._literal_constants(tree)
        name = constants.get("EXTENSION_NAME")
        priority = constants.get("PRIORITY")
        manifest_raw = constants.get("EXTENSION_MANIFEST")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Extension requires literal EXTENSION_NAME")
        if isinstance(priority, bool) or not isinstance(priority, int) or not -50 <= priority <= 50:
            raise ValueError("Extension PRIORITY must be in -50..50")
        if not isinstance(manifest_raw, dict):
            raise ValueError("Extension requires literal EXTENSION_MANIFEST")
        manifest = ExtensionManifest.model_validate(manifest_raw)
        module_name = (
            f"_yuan_ye_extension_{point.value}_{resolved.stem}_"
            f"{hashlib.sha256(source).hexdigest()[:16]}"
        )
        spec = importlib.util.spec_from_file_location(module_name, resolved)
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot create Extension module: {resolved}")
        module = importlib.util.module_from_spec(spec)
        try:
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
        imported_manifest = ExtensionManifest.model_validate(
            getattr(module, "EXTENSION_MANIFEST", None),
        )
        if imported_manifest != manifest:
            raise ValueError("Imported Extension Manifest differs from AST literal")
        handle = getattr(module, "handle", None)
        if getattr(module, "EXTENSION_NAME", None) != name or getattr(module, "PRIORITY", None) != priority:
            raise ValueError("Imported Extension identity differs from AST literal")
        if not inspect.iscoroutinefunction(handle):
            raise ValueError("Extension handle must be async def")
        parameters = list(inspect.signature(handle).parameters.values())
        if len(parameters) != 2 or [item.name for item in parameters] != ["event", "context"]:
            raise ValueError("Extension handle signature must be (event, context)")
        source_hash = hashlib.sha256(source).hexdigest()
        manifest_hash = hashlib.sha256(manifest.canonical_json().encode("utf-8")).hexdigest()
        return ExtensionModule(
            stage=point, name=name.strip(), priority=priority, path=resolved, handle=handle,
            manifest=manifest, source_hash=source_hash, manifest_hash=manifest_hash,
            hook_id=f"extension:{point.value}:{name.strip()}",
        )

    @staticmethod
    def _literal_constants(tree: ast.Module) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
            value = statement.value
            if isinstance(target, ast.Name) and target.id in {
                "EXTENSION_NAME", "PRIORITY", "EXTENSION_MANIFEST",
            }:
                try:
                    result[target.id] = ast.literal_eval(value)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{target.id} must be a static literal") from exc
        return result

    @staticmethod
    def _security_scan(tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    if any(name == denied or name.startswith(denied + ".") for denied in _DENIED_IMPORTS):
                        raise ValueError(f"Extension import is not allowed: {name}")
            if isinstance(node, ast.Call):
                name = ExtensionLoader._call_name(node.func)
                if name in _DENIED_CALLS or name in _DENIED_ATTRIBUTES:
                    raise ValueError(f"Extension call is not allowed: {name}")
            if isinstance(node, ast.Attribute) and ExtensionLoader._call_name(node) == "os.environ":
                raise ValueError("Extension environment access is not allowed")
            if isinstance(node, ast.Name) and node.id == "__builtins__":
                raise ValueError("Extension builtins reflection is not allowed")
            if (
                isinstance(node, ast.Attribute)
                and ExtensionLoader._root_name(node) == "context"
                and node.attr.startswith("_")
            ):
                raise ValueError("Extension cannot access private Context attributes")
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(ExtensionLoader._root_name(target) == "event" for target in targets):
                    raise ValueError(
                        "Extension Event is read-only; use ExtensionContext mutation methods"
                    )

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = ExtensionLoader._call_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    @staticmethod
    def _root_name(node: ast.AST) -> str:
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else ""


def build_extension_grant_plan(
    catalog: ExtensionCatalog, tool_registry: Any,
    *, selected_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Deterministic candidate projection consumed by /code and Gateway grants."""
    hooks: list[dict[str, Any]] = []
    for modules in catalog.modules.values():
        for module in modules:
            relative_path = f"extension/hook/{module.stage.value}/{module.path.name}"
            if selected_paths is not None and relative_path not in selected_paths:
                continue
            requested = list(module.manifest.capabilities)
            tools: list[dict[str, Any]] = []
            for name in module.manifest.allowed_tools:
                tools.append({
                    "name": name,
                    "preapprovable": tool_registry.extension_preapproval_allowed(name),
                    "contract_hash": tool_registry.tool_contract_hash(name),
                    "risk": tool_registry.risk_of(name),
                })
            hooks.append({
                "hook_id": module.hook_id,
                "path": relative_path,
                "stage": module.stage.value,
                "source_hash": module.source_hash,
                "manifest_hash": module.manifest_hash,
                "requested_capabilities": [item.value for item in requested],
                "auto_granted_capabilities": [
                    item.value for item in requested if item.tier is CapabilityTier.SAFE
                ],
                "confirmation_required_capabilities": [
                    item.value for item in requested if item.tier is not CapabilityTier.SAFE
                ],
                "tools": tools,
                "timeout_seconds": module.manifest.timeout_seconds,
            })
    hooks.sort(key=lambda item: (item["stage"], item["hook_id"]))
    payload = {"schema_version": 1, "hooks": hooks, "restart_required": True}
    payload["plan_hash"] = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return payload


__all__ = [
    "CapabilityTier", "ExtensionCapability", "ExtensionCapabilityDenied",
    "ExtensionCatalog", "ExtensionContext", "ExtensionContractViolation",
    "ExtensionEventView", "ExtensionLoader", "ExtensionManifest", "ExtensionModule",
    "ExtensionMutationBuffer", "ExtensionRuntimeBinding", "ExtensionRuntimePolicy",
    "ExtensionServices", "ExtensionTraceSnapshot", "build_extension_grant_plan",
]
