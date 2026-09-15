"""Memory 持久化格式的 Pydantic 数据契约。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .provider_context import ProviderContextRecord


class SessionRecord(BaseModel):
    """一行可恢复、可审计的 Session JSONL 记录。"""

    model_config = ConfigDict(extra="allow", strict=True)

    role: Literal["user", "assistant", "tool", "summary", "extension", "provider_context"]
    content: str | None
    timestamp: str = Field(min_length=1)
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    origin: Literal["interactive", "cron", "maintenance", "extension"] | None = None
    record_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    operation_id: str | None = Field(default=None, min_length=1)
    provider_context: ProviderContextRecord | None = None
    context_target_record_id: str | None = None

    @model_validator(mode="after")
    def _validate_role_payload(self) -> "SessionRecord":
        """按角色约束正文和工具关联字段，防止坏记录进入历史。"""
        if self.provider_context is not None:
            if self.role not in {"user", "provider_context"}:
                raise ValueError("Only user/context records may carry a provider projection")
            if self.role == "user":
                self.provider_context.render(self.content or "")
        if self.role == "provider_context":
            if self.content is not None or not self.context_target_record_id or self.provider_context is None:
                raise ValueError("Context amendments require a target and packet, not conversation content")
        if self.context_target_record_id and self.role != "provider_context":
            raise ValueError("Context target is only valid on context amendments")
        if self.role in {"user", "summary"} and not isinstance(self.content, str):
            raise ValueError(f"{self.role} 记录必须包含字符串 content")
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            raise ValueError("assistant 记录必须包含 content 或 tool_calls")
        if self.role == "tool":
            if not isinstance(self.content, str):
                raise ValueError("tool 记录必须包含字符串 content")
            if not self.tool_call_id or not self.name:
                raise ValueError("tool 记录必须包含 tool_call_id 和 name")
        if self.role == "extension" and not isinstance(self.content, str):
            raise ValueError("extension records require string content")
        return self


class SessionIndexEntry(BaseModel):
    """一个 Session 的文件分段索引。"""

    model_config = ConfigDict(strict=True)

    created_at: str = Field(min_length=1)
    latest_file: str = Field(min_length=1)
    files: list[str] = Field(min_length=1)
    state: Literal["pending", "active"] = "active"
    materialized_at: str | None = None
    skill_catalog: dict[str, Any] | None = None

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
