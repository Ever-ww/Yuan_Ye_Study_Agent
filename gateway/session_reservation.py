"""Shared per-session serialization for Runtime, Finalize, Recovery, and refresh."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


SessionKey = tuple[str, str]


class SessionReservationConflict(RuntimeError):
    pass


class SessionReservationRegistry:
    """A lightweight single-Gateway keyed reservation registry."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._owners: dict[SessionKey, str] = {}
        self._owner_keys: dict[str, set[SessionKey]] = {}
        self._depths: dict[tuple[str, SessionKey], int] = {}

    async def acquire(
        self,
        project_id: str,
        session_id: str,
        *,
        owner_id: str,
        wait: bool = True,
    ) -> SessionKey:
        key = (project_id, session_id)
        async with self._condition:
            while True:
                owner = self._owners.get(key)
                if owner is None or owner == owner_id:
                    self._owners[key] = owner_id
                    self._owner_keys.setdefault(owner_id, set()).add(key)
                    depth_key = (owner_id, key)
                    self._depths[depth_key] = self._depths.get(depth_key, 0) + 1
                    return key
                if not wait:
                    raise SessionReservationConflict(
                        "同一个 Session 同时只能由一个 Run 修改",
                    )
                await self._condition.wait()

    async def bind(
        self,
        project_id: str,
        session_id: str,
        *,
        owner_id: str,
        wait: bool = False,
    ) -> SessionKey:
        return await self.acquire(
            project_id, session_id, owner_id=owner_id, wait=wait,
        )

    async def release_owner(self, owner_id: str) -> None:
        async with self._condition:
            for key in self._owner_keys.pop(owner_id, set()):
                if self._owners.get(key) == owner_id:
                    self._owners.pop(key, None)
                self._depths.pop((owner_id, key), None)
            self._condition.notify_all()

    async def release(self, key: SessionKey, *, owner_id: str) -> None:
        """Release one reentrant level without dropping an outer reservation."""
        async with self._condition:
            depth_key = (owner_id, key)
            depth = self._depths.get(depth_key, 0)
            if depth <= 1:
                self._depths.pop(depth_key, None)
                if self._owners.get(key) == owner_id:
                    self._owners.pop(key, None)
                keys = self._owner_keys.get(owner_id)
                if keys is not None:
                    keys.discard(key)
                    if not keys:
                        self._owner_keys.pop(owner_id, None)
                self._condition.notify_all()
                return
            self._depths[depth_key] = depth - 1

    async def is_reserved(self, project_id: str, session_id: str) -> bool:
        async with self._condition:
            return (project_id, session_id) in self._owners

    async def owner(self, project_id: str, session_id: str) -> str | None:
        async with self._condition:
            return self._owners.get((project_id, session_id))

    async def has_any(self) -> bool:
        async with self._condition:
            return bool(self._owners)

    @property
    def busy_keys(self) -> frozenset[SessionKey]:
        """Best-effort single-event-loop snapshot for UI/maintenance checks."""
        return frozenset(self._owners)

    @asynccontextmanager
    async def reserve(
        self,
        project_id: str,
        session_id: str,
        *,
        owner_id: str,
        wait: bool = True,
    ) -> AsyncIterator[SessionKey]:
        key = await self.acquire(
            project_id, session_id, owner_id=owner_id, wait=wait,
        )
        try:
            yield key
        finally:
            await self.release(key, owner_id=owner_id)


__all__ = [
    "SessionKey",
    "SessionReservationConflict",
    "SessionReservationRegistry",
]
