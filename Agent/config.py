"""运行配置加载：仅支持项目内的 JSON 配置层。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from bootstrap import ensure_project_initialized


_JSON_OBJECT = TypeAdapter(dict[str, Any])


class RuntimeConfig(BaseModel):
    """核心运行时的最小且明确配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_root: Path
    workspace_root: Path
    coding_source_root: Path | None = None
    model: str = Field(default="echo", min_length=1)
    provider: str = Field(default="echo", min_length=1)
    base_url: str | None = None
    api_key: str | None = None
    web_search_api_key: str | None = None
    web_search_timeout_seconds: StrictInt = Field(default=20, ge=5, le=60)
    web_fetch_timeout_seconds: StrictInt = Field(default=20, ge=5, le=60)
    web_fetch_max_bytes: StrictInt = Field(default=2_000_000, ge=100_000, le=5_000_000)
    web_fetch_max_chars: StrictInt = Field(default=30_000, ge=1_000, le=30_000)
    paper_download_timeout_seconds: StrictInt = Field(default=60, ge=5, le=180)
    paper_download_max_bytes: StrictInt = Field(
        default=50_000_000,
        ge=1_000_000,
        le=200_000_000,
    )
    use_system_proxy: StrictBool = False
    proxy_url: str | None = None
    stream: StrictBool = False
    max_steps: StrictInt = Field(default=8, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    profile: str = Field(default="general", min_length=1)
    compression_threshold_tokens: StrictInt = Field(default=200000, ge=0)
    tool_output_max_chars: StrictInt = Field(default=10000, ge=0)
    tool_output_head_ratio: float = Field(default=0.20, ge=0.0, le=1.0)
    tool_output_tail_ratio: float = Field(default=0.20, ge=0.0, le=1.0)
    sandbox_checkpoint_limit: StrictInt = Field(default=17, ge=1)
    gateway_port: StrictInt = Field(default=8765, ge=1024, le=65535)
    gateway_max_concurrent_runs: StrictInt = Field(default=4, ge=1, le=32)
    gateway_runtime_idle_seconds: StrictInt = Field(default=900, ge=30)
    approval_timeout_seconds: StrictInt = Field(default=30, ge=5, le=3600)
    model_retry_max_attempts: StrictInt = Field(default=3, ge=1, le=20)
    model_retry_base_seconds: float = Field(default=2.0, ge=0.0, le=300.0)
    model_retry_max_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)
    tool_retry_max_attempts: StrictInt = Field(default=3, ge=1, le=20)
    tool_retry_base_seconds: float = Field(default=2.0, ge=0.0, le=300.0)
    tool_retry_max_seconds: float = Field(default=60.0, ge=0.0, le=3600.0)
    outbox_retry_max_attempts: StrictInt = Field(default=12, ge=1, le=100)
    outbox_retry_base_seconds: float = Field(default=2.0, ge=0.1, le=300.0)
    outbox_retry_max_seconds: float = Field(default=900.0, ge=1.0, le=86400.0)
    outbox_dead_letter_enabled: StrictBool = True
    cron_heartbeat_seconds: StrictInt = Field(default=60, ge=5)
    dream_enabled: StrictBool = True
    dream_schedule: str = Field(default="0 3 * * *", min_length=1, max_length=100)
    dream_timezone: str = Field(default="local", min_length=1, max_length=100)
    dream_model: str | None = Field(default=None, min_length=1)
    dream_batch_tokens: StrictInt = Field(default=12000, ge=1000, le=200000)
    reference_search_mode: Literal["rrf", "weighted", "separate"] = "rrf"
    reference_embedding_model: str = ""
    reference_embedding_base_url: str | None = None
    reference_embedding_api_key: str | None = None
    reference_keyword_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    reference_semantic_weight: float = Field(default=0.6, ge=0.0, le=1.0)

    @field_validator("agent_root", "workspace_root", "coding_source_root")
    @classmethod
    def _resolve_project_root(cls, value: Path | None) -> Path | None:
        """在配置边界统一工作区为绝对路径。"""
        return value.resolve() if value is not None else None

    @property
    def memory_dir(self) -> Path:
        """返回唯一的项目本地记忆目录。"""
        return self.agent_root / ".yy" / "memory"

    @property
    def reference_database_path(self) -> Path:
        return self.agent_root / ".yy" / "reference" / "reference.sqlite3"

    @field_validator("tool_output_tail_ratio")
    @classmethod
    def _validate_tool_output_ratios(cls, value: float, info) -> float:
        """首尾保留比例总和不得超过完整工具输出。"""
        head = info.data.get("tool_output_head_ratio", 0.20)
        if head + value > 1.0:
            raise ValueError("tool_output_head_ratio 与 tool_output_tail_ratio 之和不能超过 1")
        return value

    @model_validator(mode="after")
    def _validate_proxy_configuration(self) -> "RuntimeConfig":
        """显式代理与系统代理二选一，默认完全忽略代理环境变量。"""
        if self.use_system_proxy and self.proxy_url:
            raise ValueError("use_system_proxy 与 proxy_url 不能同时启用")
        if self.proxy_url and not self.proxy_url.startswith(("http://", "https://")):
            raise ValueError("proxy_url 目前只支持 http:// 或 https://")
        if self.reference_keyword_weight + self.reference_semantic_weight <= 0:
            raise ValueError("reference_keyword_weight 与 reference_semantic_weight 之和必须大于 0")
        if self.reference_embedding_base_url and not self.reference_embedding_base_url.startswith(("http://", "https://")):
            raise ValueError("reference_embedding_base_url 只支持 http:// 或 https://")
        from croniter import croniter
        from zoneinfo import ZoneInfo
        from tzlocal import get_localzone_name

        if len(self.dream_schedule.split()) != 5 or not croniter.is_valid(self.dream_schedule):
            raise ValueError("dream_schedule 必须是合法的五段 Cron 表达式")
        try:
            ZoneInfo(get_localzone_name() if self.dream_timezone == "local" else self.dream_timezone)
        except Exception as exc:
            raise ValueError(f"dream_timezone 不是有效时区：{self.dream_timezone}") from exc
        return self


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
    sensitive_keys = {"api_key", "web_search_api_key", "reference_embedding_api_key"}.intersection(shared)
    if sensitive_keys:
        raise ValueError(
            "禁止在 .yy/settings.json 保存 API Key；请移至已忽略的 .yy/settings.local.json",
        )
    values.update(shared)
    values.update(_read_json(selected_agent_root / ".yy" / "settings.local.json"))
    values.update({key: value for key, value in overrides.items() if value is not None})
    if not values.get("coding_source_root"):
        marker = _read_json(selected_agent_root / ".yy" / "agent-home-migration.json")
        source_root = marker.get("source_root")
        marker_source = Path(source_root) if isinstance(source_root, str) and source_root else None
        values["coding_source_root"] = (
            marker_source
            if marker_source is not None and marker_source.exists()
            else Path(__file__).resolve().parents[1]
        )
    # 配置文件不能改变状态目录与本轮文件操作边界。
    values["agent_root"] = selected_agent_root
    values["workspace_root"] = selected_workspace
    return RuntimeConfig.model_validate(values)


def default_agent_root() -> Path:
    """返回统一状态容器；正式运行态固定写入 `<用户目录>/.yy`。"""
    from bootstrap import (
        legacy_gateway_active,
        legacy_platform_agent_home,
        migrate_source_home,
        platform_agent_home,
    )

    source_root = Path(__file__).resolve().parents[1]
    selected = platform_agent_home()
    legacy = legacy_platform_agent_home()
    canonical_initialized = (selected / ".yy" / ".initialized.json").is_file()
    legacy_has_state = (legacy / ".yy").is_dir() or (legacy / "skills").is_dir()
    if (
        not canonical_initialized
        and legacy != selected
        and legacy_has_state
        and legacy_gateway_active(legacy)
    ):
        # 升级过程中旧 Gateway 仍可能写 SQLite。先继续返回旧位置，用户执行
        # gateway stop 后，下一次启动再安全迁移并切换到 ~/.yy。
        return legacy
    if legacy != selected and legacy_has_state:
        migrate_source_home(legacy, selected)
    migrate_source_home(source_root, selected)
    return selected
