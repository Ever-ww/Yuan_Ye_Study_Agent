"""从旧版 INI 部署配置加载模型选择和供应商覆盖项。

该模块保留对原项目 ``config.ini`` 的兼容读取。新 Harness 可以在更高层合并
``.yy/settings*.json``，而这里仍专注于模型组件自己的最小配置，不负责项目级
配置优先级。缺少配置文件时返回安全默认值，不会隐式创建或修改文件。
"""

from __future__ import annotations

import os
from configparser import ConfigParser
from dataclasses import dataclass, replace
from pathlib import Path

from .config import ProviderConfig, PROVIDERS, resolve_model

# ``model_choice`` 是可复用组件，而旧版项目配置位于仓库根目录；使用绝对 Path
# 可避免调用命令时的当前工作目录影响默认配置定位。
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.ini"


@dataclass(frozen=True)
class AppSettings:
    """经过解析的旧版应用级模型配置。

    Attributes:
        default_model: 未显式指定模型时使用的别名或限定名。
        timeout_seconds: 单次供应商请求的超时。
        provider_overrides: 按供应商保存 ``base_url``、``api_key_env``，以及为
            兼容旧配置而保留的可选 ``api_key``。
        fallback_model: 可选备用模型名称；实际切换由运行时 Provider 负责。

    推荐只在配置中保存密钥环境变量名。``api_key`` 字段虽为向后兼容而存在，
    但可提交的配置文件不应包含真实凭据。
    """

    default_model: str
    timeout_seconds: float
    provider_overrides: dict[str, dict[str, str]]
    fallback_model: str | None = None

    def resolve_model(self, model: str | None = None) -> tuple[ProviderConfig, str]:
        """解析模型，并将对应供应商的部署覆盖项应用到配置副本。

        本次调用传入的 ``model`` 高于 ``default_model``；供应商覆盖只允许修改
        API 根地址和密钥环境变量名，不能悄悄把模型路由到另一个供应商。末尾
        斜杠会被移除，以保证客户端拼接端点时不会形成双斜杠。
        """

        config, resolved_model = resolve_model(model or self.default_model)
        override = self.provider_overrides.get(config.provider.value, {})
        return replace(
            config,
            base_url=override.get("base_url", config.base_url).rstrip("/"),
            api_key_env=override.get("api_key_env", config.api_key_env),
        ), resolved_model

    def api_key_for(self, provider: str) -> str | None:
        """返回旧配置中显式填写的密钥，未填写时返回 ``None``。

        这是兼容路径而非推荐的秘密管理方式。客户端优先使用调用者显式传入的
        密钥；这里没有值时才继续查询环境变量或由 Harness 查询系统凭据库。
        """
        key = self.provider_overrides.get(provider, {}).get("api_key", "").strip()
        return key or None


def load_settings(path: str | Path | None = None) -> AppSettings:
    """读取 INI 配置并生成不可变的 :class:`AppSettings`。

    配置路径优先级为：函数参数 > ``MODEL_CHOICE_CONFIG`` 环境变量 > 仓库根目录
    ``config.ini``。文件缺失不是错误，此时使用 ``gpt``、60 秒超时等默认值。
    Provider 小节必须写成 ``[providers.<name>]``，未知供应商会立即报错，避免
    拼写错误导致配置被静默忽略。每个小节只采纳白名单字段，其余键不会进入
    运行时配置。

    Raises:
        ValueError: 文件存在但无法读取、包含未知供应商，或数值字段无法转换。
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
