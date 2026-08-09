"""Skill 获取、审核和安装状态的 Pydantic 数据契约。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SkillMetadata(BaseModel):
    """可注入 System Prompt 的已审核 Skill 元数据。"""

    model_config = ConfigDict(frozen=True, strict=True)

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    location: str = Field(min_length=1)
    license: str | None = Field(default=None, min_length=1)
    compatibility: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: str | None = Field(default=None, min_length=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillCatalogSnapshot(BaseModel):
    """某个 Session 固定使用的仓库 Skill 目录快照。"""

    model_config = ConfigDict(frozen=True, strict=True)

    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skills: tuple[SkillMetadata, ...] = ()

    @field_validator("skills", mode="before")
    @classmethod
    def _restore_json_tuple(cls, value):
        """JSON 数组是 tuple 的持久化表示，恢复时重新冻结目录顺序。"""
        return tuple(value) if isinstance(value, list) else value

    def by_name(self) -> dict[str, SkillMetadata]:
        return {item.name: item for item in self.skills}


class SkillSource(BaseModel):
    """一次审核使用的不可变来源信息。"""

    model_config = ConfigDict(frozen=True, strict=True)

    kind: Literal["github", "local", "builtin"]
    value: str = Field(min_length=1)
    ref: str | None = None
    skill_path: str | None = None
    commit: str | None = None


class SkillAuditFinding(BaseModel):
    """静态审核发现的一条风险或说明。"""

    model_config = ConfigDict(frozen=True, strict=True)

    severity: Literal["block", "review", "info"]
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None


class SkillAuditReport(BaseModel):
    """保存在 `.yy` 的完整审核报告。"""

    model_config = ConfigDict(frozen=True, strict=True)

    review_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    status: Literal["blocked", "review_required", "clean", "installed", "declined"]
    source: SkillSource
    skill: SkillMetadata | None = None
    findings: tuple[SkillAuditFinding, ...] = ()
    files: dict[str, str] = Field(default_factory=dict)
    total_files: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    report_path: Path


class SkillInstallRequest(BaseModel):
    """CLI 与工具共用的安装请求。"""

    model_config = ConfigDict(frozen=True, strict=True)

    source: str = Field(min_length=1, max_length=4096)
    action: Literal["install", "update"] = "install"
    ref: str | None = Field(default=None, max_length=255)
    skill_path: str | None = Field(default=None, max_length=1024)
    name: str | None = Field(default=None, max_length=64)

    @field_validator("source")
    @classmethod
    def _strip_source(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source 不能为空")
        return stripped

    @field_validator("ref", "skill_path", "name")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Skill 名称格式无效")
        return value


class SkillInstallResult(BaseModel):
    """安装流水线返回给 CLI 或模型的稳定结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    status: Literal[
        "installed",
        "blocked",
        "declined",
        "selection_required",
        "conflict",
        "error",
    ]
    message: str
    review_id: str | None = None
    name: str | None = None
    candidates: tuple[str, ...] = ()
    report_path: Path | None = None


class SkillRefreshResult(BaseModel):
    """当前 Session 刷新仓库 Skill 目录后的事务结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    status: Literal["refreshed", "unchanged", "error"]
    message: str
    session_id: str
    count: int = Field(default=0, ge=0)
    old_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    new_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    source_file: str | None = None
    target_file: str | None = None


class InstalledSkillEntry(BaseModel):
    """`.yy/skills/index.json` 中一个已审核发布 Skill 的来源和摘要。"""

    model_config = ConfigDict(strict=True)

    status: Literal["installed"] = "installed"
    name: str
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str
    source: SkillSource
    review_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    installed_at: datetime


class SkillIndex(BaseModel):
    """已审核发布 Skill 的来源与审计索引；不作为运行时正文目录。"""

    model_config = ConfigDict(strict=True)

    version: Literal[1] = 1
    skills: dict[str, InstalledSkillEntry] = Field(default_factory=dict)
