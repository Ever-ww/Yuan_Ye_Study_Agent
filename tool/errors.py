"""工具请求和确定失败的结构化错误边界。"""

from __future__ import annotations


class ToolRequestError(RuntimeError):
    """真实副作用开始前发现的参数、权限或可用性错误。"""


class ToolExecutionObservationError(RuntimeError):
    """Ledger 已确认执行失败，可安全交还模型重新规划。"""


__all__ = ["ToolExecutionObservationError", "ToolRequestError"]
