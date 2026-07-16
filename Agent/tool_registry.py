"""旧同步工具注册表的兼容转发模块。

``ToolRegistry`` 的实现已移至 :mod:`Agent.legacy`。异步 Harness 请使用
``tools.harness.ToolRegistry``；两者协议不同，不能互换。
"""

from .legacy import ToolRegistry

__all__ = ["ToolRegistry"]
