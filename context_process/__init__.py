"""上下文压缩、分段和失败降级公共入口。"""

from .compression import CompressionResult, ContextProcessor
from .callbacks import register_context_callbacks

__all__ = ["CompressionResult", "ContextProcessor", "register_context_callbacks"]
