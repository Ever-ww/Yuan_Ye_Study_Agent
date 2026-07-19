"""运行配置加载：仅支持项目内的 JSON 配置层。"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from bootstrap import ensure_project_initialized


@dataclass(frozen=True)
class RuntimeConfig:
    """核心运行时的最小且明确配置。"""

    project_root: Path
    model: str = "echo"
    provider: str = "echo"
    base_url: str | None = None
    api_key: str | None = None
    stream: bool = False
    max_steps: int = 8
    temperature: float = 0.0
    profile: str = "general"

    @property
    def memory_dir(self) -> Path:
        """返回唯一的项目本地记忆目录。"""
        return self.project_root / ".yy" / "memory"


def _read_json(path: Path) -> dict[str, Any]:
    """读取可选 JSON 对象；缺失配置等价于空配置。"""
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象：{path}")
    return value


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
    if "stream" in values and not isinstance(values["stream"], bool):
        raise ValueError("stream 必须是 true 或 false")
    allowed = {item.name for item in fields(RuntimeConfig)}
    return RuntimeConfig(**{key: value for key, value in values.items() if key in allowed})
