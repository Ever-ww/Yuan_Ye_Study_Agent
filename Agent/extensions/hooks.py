"""Hook 注册和顺序执行，不提供外部脚本执行能力。"""

from __future__ import annotations

from typing import Any, Protocol


class Hook(Protocol):
    """Hook 可观察或改写工具参数，但不能绕过权限判断。"""

    async def before_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class HookRegistry:
    """按注册顺序运行纯 Python Hook。"""

    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self._hooks = hooks or []

    async def before_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """逐个执行 Hook，拒绝非对象参数。"""
        current = dict(arguments)
        for hook in self._hooks:
            current = await hook.before_tool(name, current)
            if not isinstance(current, dict):
                raise ValueError("Hook 必须返回 JSON 对象")
        return current
