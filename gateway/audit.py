"""控制面审计数据的统一脱敏入口。"""

from __future__ import annotations

import re
from typing import Any


class AuditSanitizer:
    """递归清理事件、操作与错误中可能出现的凭据。"""

    _SENSITIVE_KEY = re.compile(
        r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|access[_-]?token|refresh[_-]?token)",
        re.IGNORECASE,
    )
    _INLINE = (
        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
        re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+"),
    )

    @classmethod
    def sanitize(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]" if cls._SENSITIVE_KEY.search(str(key)) else cls.sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [cls.sanitize(item) for item in value]
        if isinstance(value, str):
            selected = value
            for pattern in cls._INLINE:
                selected = pattern.sub(r"\1[REDACTED]", selected)
            return selected
        return value


__all__ = ["AuditSanitizer"]
