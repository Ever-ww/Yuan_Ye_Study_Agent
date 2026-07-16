"""分层 System Prompt 组合功能的稳定公共入口。

调用者通常只需导入 :class:`PromptComposer`；片段数据类型保留在内部模块中，
由 ``compose`` 返回并供 ``inspect`` 使用。
"""

from .composer import PromptComposer

__all__ = ["PromptComposer"]
