"""旧版同步 ReAct 循环的 IANA 时区时间查询工具。

返回带 UTC 偏移的 ISO 8601 时间，避免在会话、Cron 或日志中
产生无法确定时区的本地时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class CurrentTimeTool:
    """查询指定 IANA 时区的当前时间。

    时区名由标准库 :class:`zoneinfo.ZoneInfo` 解析；无效名称的异常
    会交给上层统一转换为工具错误。
    """

    name: str = "current_time"
    description: str = "查询指定时区的当前时间。"
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"timezone": {"type": "string", "description": "IANA 时区，如 Asia/Shanghai"}},
    })

    def run(self, arguments: dict[str, Any]) -> str:
        """返回秒精度、包含偏移量的 ISO 8601 时间字符串。"""

        timezone = arguments.get("timezone", "Asia/Shanghai")
        return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
