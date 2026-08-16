"""Gateway-only MANUAL Harness entry adapter used by `/code`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class ManualHarnessService(Protocol):
    async def start(self, *args: Any, **kwargs: Any) -> Any: ...
    async def run_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    async def finalize(self, *args: Any, **kwargs: Any) -> Any: ...
    async def abort(self, *args: Any, **kwargs: Any) -> Any: ...


class HarnessManualTool:
    """Internal adapter, deliberately absent from every model Tool Registry."""

    name = "harness_manual"
    runtime_profiles: tuple[str, ...] = ()
    delegatable = False

    def __init__(
        self, service: ManualHarnessService | Callable[[], ManualHarnessService],
    ) -> None:
        self._service = service

    @property
    def service(self) -> ManualHarnessService:
        return self._service() if callable(self._service) else self._service

    async def start(self, *args: Any, **kwargs: Any) -> Any:
        return await self.service.start(*args, **kwargs)

    async def turn(self, *args: Any, **kwargs: Any) -> Any:
        return await self.service.run_turn(*args, **kwargs)

    async def finalize(self, *args: Any, **kwargs: Any) -> Any:
        return await self.service.finalize(*args, **kwargs)

    async def abort(self, *args: Any, **kwargs: Any) -> Any:
        return await self.service.abort(*args, **kwargs)


__all__ = ["HarnessManualTool"]
