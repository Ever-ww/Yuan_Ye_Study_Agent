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

    agent_root: Path
    workspace_root: Path
    model: str = Field(default="echo", min_length=1)
    provider: str = Field(default="echo", min_length=1)
    base_url: str | None = None
    api_key: str | None = None
    stream: StrictBool = False
    max_steps: StrictInt = Field(default=8, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    profile: str = Field(default="general", min_length=1)
    compression_threshold_tokens: StrictInt = Field(default=20000, ge=0)
    tool_output_max_chars: StrictInt = Field(default=10000, ge=0)
    tool_output_head_ratio: float = Field(default=0.20, ge=0.0, le=1.0)
    tool_output_tail_ratio: float = Field(default=0.20, ge=0.0, le=1.0)
    sandbox_checkpoint_limit: StrictInt = Field(default=17, ge=1)
    gateway_port: StrictInt = Field(default=8765, ge=1024, le=65535)
    gateway_max_concurrent_runs: StrictInt = Field(default=4, ge=1, le=32)
    gateway_runtime_idle_seconds: StrictInt = Field(default=900, ge=30)

    @field_validator("agent_root", "workspace_root")
    @classmethod
    def _resolve_project_root(cls, value: Path) -> Path:
        """在配置边界统一工作区为绝对路径。"""
        return value.resolve()

    @property
    def memory_dir(self) -> Path:
        """返回唯一的项目本地记忆目录。"""
        return self.agent_root / ".yy" / "memory"

    @field_validator("tool_output_tail_ratio")
    @classmethod
    def _validate_tool_output_ratios(cls, value: float, info) -> float:
        """首尾保留比例总和不得超过完整工具输出。"""
        head = info.data.get("tool_output_head_ratio", 0.20)
        if head + value > 1.0:
            raise ValueError("tool_output_head_ratio 与 tool_output_tail_ratio 之和不能超过 1")
        return value


def _read_json(path: Path) -> dict[str, Any]:
    """读取可选 JSON 对象；缺失配置等价于空配置。"""
    if not path.exists():
        return {}
    try:
        return _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"), strict=True)
    except ValidationError as exc:
        raise ValueError(f"配置必须是合法 JSON 对象：{path}\n{exc}") from exc


def load_runtime_config(
    agent_root: Path | None = None,
    *,
    workspace_root: Path | None = None,
    **overrides: Any,
) -> RuntimeConfig:
    """加载 Agent 本机状态，并把启动目录作为独立工作区。"""
    selected_agent_root = (agent_root or default_agent_root()).resolve()
    selected_workspace = (
        workspace_root.resolve()
        if workspace_root is not None
        else (selected_agent_root if agent_root is not None else Path.cwd().resolve())
    )
    ensure_project_initialized(selected_agent_root)
    values: dict[str, Any] = {}
    shared = _read_json(selected_agent_root / ".yy" / "settings.json")
    if "api_key" in shared:
        raise ValueError("禁止在 .yy/settings.json 保存 api_key；请移至已忽略的 .yy/settings.local.json")
    values.update(shared)
    values.update(_read_json(selected_agent_root / ".yy" / "settings.local.json"))
    values.update({key: value for key, value in overrides.items() if value is not None})
    # 配置文件不能改变状态目录与本轮文件操作边界。
    values["agent_root"] = selected_agent_root
    values["workspace_root"] = selected_workspace
    return RuntimeConfig.model_validate(values)


def default_agent_root() -> Path:
    """返回安装版统一 Agent Home，并非当前用户 workspace。"""
    from bootstrap import migrate_source_home, platform_agent_home

    source_root = Path(__file__).resolve().parents[1]
    selected = platform_agent_home()
    migrate_source_home(source_root, selected)
    return selected
