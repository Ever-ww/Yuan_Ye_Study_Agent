"""Gateway-only DREAM Harness entry adapter."""

from __future__ import annotations

from typing import Any, Protocol


class DreamHarnessService(Protocol):
    async def run_harness_dream(self, selected: str | None, *, automatic: bool, actor: str) -> Any: ...
    def harness_dream_status(self) -> Any: ...
    def freeze_harness_dream(self, *, actor: str, reason: str) -> Any: ...
    def unfreeze_harness_dream(self) -> Any: ...
    async def create_harness_dream_revert(self, operation_id: str, *, actor: str) -> Any: ...


class HarnessDreamTool:
    name = "harness_dream"
    runtime_profiles: tuple[str, ...] = ()
    delegatable = False

    def __init__(self, service: DreamHarnessService) -> None:
        self.service = service

    async def run(self, selected: str | None, *, automatic: bool, actor: str) -> Any:
        return await self.service.run_harness_dream(selected, automatic=automatic, actor=actor)

    def status(self) -> Any:
        return self.service.harness_dream_status()

    def freeze(self, *, actor: str, reason: str) -> Any:
        return self.service.freeze_harness_dream(actor=actor, reason=reason)

    def unfreeze(self) -> Any:
        return self.service.unfreeze_harness_dream()

    async def revert(self, operation_id: str, *, actor: str) -> Any:
        return await self.service.create_harness_dream_revert(operation_id, actor=actor)


__all__ = ["HarnessDreamTool"]
