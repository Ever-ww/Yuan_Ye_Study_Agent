"""从部署配置文件加载模型选择与供应商覆盖项。"""

from __future__ import annotations

import os
from configparser import ConfigParser
from dataclasses import dataclass, replace
from pathlib import Path

from .config import ProviderConfig, PROVIDERS, resolve_model

# ``model_choice`` is a shared component; project-level settings live at the repository root.
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.ini"


@dataclass(frozen=True)
class AppSettings:
    """应用级模型配置。密钥名称而非密钥内容保存在配置中。"""

    default_model: str
    timeout_seconds: float
    provider_overrides: dict[str, dict[str, str]]
    fallback_model: str | None = None

    def resolve_model(self, model: str | None = None) -> tuple[ProviderConfig, str]:
        config, resolved_model = resolve_model(model or self.default_model)
        override = self.provider_overrides.get(config.provider.value, {})
        return replace(
            config,
            base_url=override.get("base_url", config.base_url).rstrip("/"),
            api_key_env=override.get("api_key_env", config.api_key_env),
        ), resolved_model

    def api_key_for(self, provider: str) -> str | None:
        """返回配置文件中的密钥；未填写时由客户端继续读取环境变量。"""
        key = self.provider_overrides.get(provider, {}).get("api_key", "").strip()
        return key or None


def load_settings(path: str | Path | None = None) -> AppSettings:
    """读取 INI 配置；文件缺失时使用安全的默认配置。

    通过 ``MODEL_CHOICE_CONFIG`` 环境变量可指定配置文件位置。
    """
    config_path = Path(path or os.getenv("MODEL_CHOICE_CONFIG", DEFAULT_CONFIG_PATH))
    parser = ConfigParser()
    if config_path.exists() and not parser.read(config_path, encoding="utf-8"):
        raise ValueError(f"无法读取配置文件：{config_path}")

    app = parser["model_choice"] if parser.has_section("model_choice") else {}

    overrides: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        if not section.startswith("providers."):
            continue
        name = section.removeprefix("providers.")
        if name not in {provider.value for provider in PROVIDERS}:
            raise ValueError(f"config.ini 包含未知供应商：{name}")
        values = parser[section]
        overrides[name] = {key: value for key, value in values.items() if key in {"base_url", "api_key_env", "api_key"}}

    return AppSettings(
        default_model=str(app.get("default_model", "gpt")),
        timeout_seconds=float(app.get("timeout_seconds", 60)),
        provider_overrides=overrides,
        fallback_model=str(app.get("fallback_model", "")).strip() or None,
    )
