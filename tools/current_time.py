"""当前时间查询工具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class CurrentTimeTool:
    name: str = "current_time"
    description: str = "查询指定时区的当前时间。"
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"timezone": {"type": "string", "description": "IANA 时区，如 Asia/Shanghai"}},
    })

    def run(self, arguments: dict[str, Any]) -> str:
        timezone = arguments.get("timezone", "Asia/Shanghai")
        return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
