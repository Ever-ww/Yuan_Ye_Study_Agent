"""Generation-selected adapters mounted on the stable Hook lifecycle bus.

These objects adapt stable Core services to a Runtime.  They do not own the
canonical Memory store, sandbox backend, HookExecutor, or context processor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from Agent.hook import (
    HookCallback,
    HookFailureMode,
    HookOrigin,
    HookPoint,
    HookRegistry,
)
from Agent.resources import (
    ResolvedRuntimeContribution,
    RuntimeContributionKind,
    RuntimePluginFailureKind,
    RuntimeResourceSnapshot,
)


class RuntimeContributionAdapter(Protocol):
    plugin_id: str
    name: str

    def install(self, registry: HookRegistry) -> None: ...


@dataclass(frozen=True)
class RuntimeHookCallbackContribution:
    """A plugin callback installed before the immutable HookPlan is frozen."""

    plugin_id: str
    name: str
    point: HookPoint
    callback: HookCallback
    priority: int = 0
    timeout_seconds: float = 30.0
    failure_mode: HookFailureMode = HookFailureMode.ISOLATE
    failure_reporter: Callable[..., Any] | None = None
    success_reporter: Callable[..., Any] | None = None

    def install(self, registry: HookRegistry) -> None:
        async def report(outcome: str, error: BaseException | None, duration: float) -> None:
            del duration
            if error is None:
                if outcome == "success" and self.success_reporter is not None:
                    result = self.success_reporter(self.plugin_id)
                    if hasattr(result, "__await__"):
                        await result
                return
            if self.failure_reporter is None:
                return
            kind = (
                RuntimePluginFailureKind.PLUGIN_TIMEOUT
                if outcome == "timeout"
                else RuntimePluginFailureKind.PLUGIN_EXCEPTION
            )
            result = self.failure_reporter(self.plugin_id, kind, error)
            if hasattr(result, "__await__"):
                await result

        registry.register(
            self.point,
            self.callback,
            priority=self.priority,
            identity=f"plugin:{self.plugin_id}:{self.name}",
            origin=HookOrigin.EXTENSION,
            failure_mode=self.failure_mode,
            timeout_seconds=self.timeout_seconds,
            outcome_reporter=report,
        )


@dataclass(frozen=True)
class MemoryRuntimeAdapter:
    plugin_id: str
    name: str
    memory: Any
    prompts: Any
    session_origin: str
    runtime_profile: str

    def install(self, registry: HookRegistry) -> None:
        from memory.callbacks import register_memory_callbacks

        register_memory_callbacks(
            registry,
            self.memory,
            self.prompts,
            session_origin=self.session_origin,
            runtime_profile=self.runtime_profile,
        )


@dataclass(frozen=True)
class DynamicContextRuntimeAdapter:
    plugin_id: str
    name: str
    memory: Any
    context_processor: Any
    runtime_config: Any
    session_read_available: bool

    def install(self, registry: HookRegistry) -> None:
        from context_process import (
            register_context_callbacks,
            register_tool_output_trimming_callbacks,
        )

        register_tool_output_trimming_callbacks(
            registry,
            self.memory,
            self.runtime_config,
            session_read_available=self.session_read_available,
        )
        if self.context_processor is not None:
            register_context_callbacks(registry, self.context_processor)


@dataclass(frozen=True)
class SandboxRuntimeAdapter:
    plugin_id: str
    name: str
    sandbox: Any

    def install(self, registry: HookRegistry) -> None:
        from sandbox.callbacks import register_sandbox_callbacks

        register_sandbox_callbacks(registry, self.sandbox)


def build_runtime_contribution_adapters(
    snapshot: RuntimeResourceSnapshot,
    *,
    memory: Any,
    prompts: Any,
    context_processor: Any,
    sandbox: Any,
    runtime_config: Any,
    session_origin: str,
    runtime_profile: str,
    session_read_available: bool,
) -> tuple[RuntimeContributionAdapter, ...]:
    """Resolve only adapters explicitly present in the Profile Snapshot."""
    adapters: list[RuntimeContributionAdapter] = []
    for contribution in snapshot.contributions:
        adapter = str(contribution.contract.get("adapter", ""))
        if adapter == "memory" and contribution.kind is RuntimeContributionKind.HOOK:
            adapters.append(MemoryRuntimeAdapter(
                contribution.plugin_id, contribution.name, memory, prompts,
                session_origin, runtime_profile,
            ))
        elif (
            adapter == "dynamic_context"
            and contribution.kind is RuntimeContributionKind.DYNAMIC_CONTEXT
        ):
            adapters.append(DynamicContextRuntimeAdapter(
                contribution.plugin_id, contribution.name, memory,
                context_processor, runtime_config, session_read_available,
            ))
        elif adapter == "sandbox" and contribution.kind is RuntimeContributionKind.HOOK:
            if sandbox is not None:
                adapters.append(SandboxRuntimeAdapter(
                    contribution.plugin_id, contribution.name, sandbox,
                ))
    return tuple(adapters)


def install_runtime_contribution_adapters(
    registry: HookRegistry,
    adapters: tuple[RuntimeContributionAdapter, ...],
) -> None:
    """Install callbacks before HookRegistry.freeze(); execution stays modular."""
    for adapter in adapters:
        adapter.install(registry)


__all__ = [
    "DynamicContextRuntimeAdapter",
    "MemoryRuntimeAdapter",
    "RuntimeContributionAdapter",
    "RuntimeHookCallbackContribution",
    "SandboxRuntimeAdapter",
    "build_runtime_contribution_adapters",
    "install_runtime_contribution_adapters",
]
