"""Memory 持久化格式的 Pydantic 数据契约。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionRecord(BaseModel):
    """一行可恢复、可审计的 Session JSONL 记录。"""

    model_config = ConfigDict(extra="allow", strict=True)

    role: Literal["user", "assistant", "tool", "summary"]
    content: str | None
    timestamp: str = Field(min_length=1)
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _validate_role_payload(self) -> "SessionRecord":
        """按角色约束正文和工具关联字段，防止坏记录进入历史。"""
        if self.role in {"user", "summary"} and not isinstance(self.content, str):
            raise ValueError(f"{self.role} 记录必须包含字符串 content")
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            raise ValueError("assistant 记录必须包含 content 或 tool_calls")
        if self.role == "tool":
            if not isinstance(self.content, str):
                raise ValueError("tool 记录必须包含字符串 content")
            if not self.tool_call_id or not self.name:
                raise ValueError("tool 记录必须包含 tool_call_id 和 name")
        return self


class SessionIndexEntry(BaseModel):
    """一个 Session 的文件分段索引。"""

    model_config = ConfigDict(strict=True)

    created_at: str = Field(min_length=1)
    latest_file: str = Field(min_length=1)
    files: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _latest_file_must_exist(self) -> "SessionIndexEntry":
        if self.latest_file not in self.files:
            raise ValueError("latest_file 必须存在于 files 中")
        return self


class SessionIndex(BaseModel):
    """`.yy/memory/session/index.json` 的完整结构。"""

    model_config = ConfigDict(strict=True)

    version: Literal[1] = 1
    sessions: dict[str, SessionIndexEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_session_ids(self) -> "SessionIndex":
        invalid = next((key for key in self.sessions if re.fullmatch(r"[0-9a-f]{16}", key) is None), None)
        if invalid is not None:
            raise ValueError(f"无效 Session 哈希：{invalid}")
        return self


class ProfileIndexEntry(BaseModel):
    """一个 Session Profile 的累计处理指标。"""

    model_config = ConfigDict(strict=True)

    file: str = Field(min_length=1)
    source_files: list[str] = Field(default_factory=list)
    segments_processed: int = Field(default=0, ge=0)
    conversation_turns: int = Field(default=0, ge=0)
    records_processed: int = Field(default=0, ge=0)
    tool_calls_processed: int = Field(default=0, ge=0)
    last_updated_at: str | None = None


class ProfileIndex(BaseModel):
    """`.yy/memory/profile/index.json` 的完整结构。"""

    model_config = ConfigDict(strict=True)

    version: Literal[1] = 1
    profiles: dict[str, ProfileIndexEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_profile_ids(self) -> "ProfileIndex":
        invalid = next((key for key in self.profiles if re.fullmatch(r"[0-9a-f]{16}", key) is None), None)
        if invalid is not None:
            raise ValueError(f"无效 Profile Session 哈希：{invalid}")
        return self
