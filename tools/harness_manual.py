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
        try:
            return await self.service.finalize(*args, **kwargs)
        except TypeError as exc:
            if not any(name in str(exc) for name in ("approved_plan_hash", "run_id")):
                raise
            fallback = dict(kwargs)
            fallback.pop("approved_plan_hash", None)
            fallback.pop("run_id", None)
            return await self.service.finalize(*args, **fallback)

    async def abort(self, *args: Any, **kwargs: Any) -> Any:
        return await self.service.abort(*args, **kwargs)


__all__ = ["HarnessManualTool"]
