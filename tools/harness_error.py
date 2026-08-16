"""Gateway-only ERROR Harness entry adapter."""

from __future__ import annotations

from typing import Any, Protocol


class ErrorHarnessService(Protocol):
    async def execute_error(self, proposal_id: str) -> dict[str, Any]: ...


class HarnessErrorTool:
    name = "harness_error"
    runtime_profiles: tuple[str, ...] = ()
    delegatable = False

    def __init__(self, service: ErrorHarnessService) -> None:
        self.service = service

    async def run_proposal(self, proposal_id: str) -> dict[str, Any]:
        return await self.service.execute_error(proposal_id)


__all__ = ["HarnessErrorTool"]
