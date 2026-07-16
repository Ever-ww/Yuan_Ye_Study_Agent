"""旧版同步 ReAct 工具的最小公共协议。

该协议刻意不依赖异步 Harness，因此旧的 :class:`Agent.agent.Agent`
仍可以使用简单的同步工具。新运行时工具则实现
``Agent.types.AsyncTool``，并由 :mod:`tools.harness` 管理。
"""

from __future__ import annotations

from typing import Any, Protocol


class Tool(Protocol):
    """定义旧版同步工具所需的结构化接口。

    ``name`` 是模型选择工具时使用的稳定标识；``description``
    和 ``parameters`` 会作为工具 Schema 提供给模型。``Protocol`` 只做
    静态结构检查，工具类无需显式继承它。
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def run(self, arguments: dict[str, Any]) -> str:
        """同步执行一次工具调用并返回可交给模型的文本。"""

        ...
