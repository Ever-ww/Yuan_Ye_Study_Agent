"""运行配置加载：仅支持项目内的 JSON 配置层。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, TypeAdapter, ValidationError, field_validator

from bootstrap import ensure_project_initialized


_JSON_OBJECT = TypeAdapter(dict[str, Any])


class RuntimeConfig(BaseModel):
    """核心运行时的最小且明确配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_root: Path
    model: str = Field(default="echo", min_length=1)
    provider: str = Field(default="echo", min_length=1)
    base_url: str | None = None
    api_key: str | None = None
    stream: StrictBool = False
    max_steps: StrictInt = Field(default=8, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    profile: str = Field(default="general", min_length=1)
    compression_threshold_tokens: StrictInt = Field(default=20000, ge=0)

    @field_validator("project_root")
    @classmethod
    def _resolve_project_root(cls, value: Path) -> Path:
        """在配置边界统一工作区为绝对路径。"""
        return value.resolve()

    @property
    def memory_dir(self) -> Path:
        """返回唯一的项目本地记忆目录。"""
        return self.project_root / ".yy" / "memory"


def _read_json(path: Path) -> dict[str, Any]:
    """读取可选 JSON 对象；缺失配置等价于空配置。"""
    if not path.exists():
        return {}
    try:
        return _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"), strict=True)
    except ValidationError as exc:
        raise ValueError(f"配置必须是合法 JSON 对象：{path}\n{exc}") from exc


def load_runtime_config(project_root: Path | None = None, **overrides: Any) -> RuntimeConfig:
    """按共享配置、本机配置和显式参数的顺序合并。"""
    root = (project_root or Path.cwd()).resolve()
    ensure_project_initialized(root)
    values: dict[str, Any] = {"project_root": root}
    shared = _read_json(root / ".yy" / "settings.json")
    if "api_key" in shared:
        raise ValueError("禁止在 .yy/settings.json 保存 api_key；请移至已忽略的 .yy/settings.local.json")
    values.update(shared)
    values.update(_read_json(root / ".yy" / "settings.local.json"))
    values.update({key: value for key, value in overrides.items() if value is not None})
    return RuntimeConfig.model_validate(values)
