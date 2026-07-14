"""供应商默认配置及模型解析。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class Provider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    ZHIPU = "zhipu"
    QWEN = "qwen"
    KIMI = "kimi"


@dataclass(frozen=True)
class ProviderConfig:
    provider: Provider
    base_url: str
    api_key_env: str
    default_model: str
    api_style: str  # "responses"、"messages" 或 "chat_completions"


PROVIDERS: dict[Provider, ProviderConfig] = {
    Provider.OPENAI: ProviderConfig(Provider.OPENAI, "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4.1-mini", "responses"),
    Provider.ANTHROPIC: ProviderConfig(Provider.ANTHROPIC, "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", "claude-sonnet-4-20250514", "messages"),
    Provider.DEEPSEEK: ProviderConfig(Provider.DEEPSEEK, "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat", "chat_completions"),
    Provider.ZHIPU: ProviderConfig(Provider.ZHIPU, "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY", "glm-4.5-air", "chat_completions"),
    Provider.QWEN: ProviderConfig(Provider.QWEN, "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "qwen-plus", "chat_completions"),
    Provider.KIMI: ProviderConfig(Provider.KIMI, "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "moonshot-v1-8k", "chat_completions"),
}


MODEL_ALIASES: dict[str, tuple[Provider, str]] = {
    "gpt": (Provider.OPENAI, "gpt-4.1-mini"),
    "claude": (Provider.ANTHROPIC, "claude-sonnet-4-20250514"),
    "deepseek": (Provider.DEEPSEEK, "deepseek-chat"),
    "glm": (Provider.ZHIPU, "glm-4.5-air"),
    "qwen": (Provider.QWEN, "qwen-plus"),
    "kimi": (Provider.KIMI, "moonshot-v1-8k"),
}


def resolve_model(name: str) -> tuple[ProviderConfig, str]:
    """解析简写（如 ``qwen``）或 ``provider:model`` 格式。"""
    normalized = name.strip().lower()
    if normalized in MODEL_ALIASES:
        provider, model = MODEL_ALIASES[normalized]
        return PROVIDERS[provider], model

    try:
        provider_name, model = normalized.split(":", maxsplit=1)
        provider = Provider(provider_name)
    except ValueError as exc:
        choices = ", ".join(MODEL_ALIASES)
        raise ValueError(f"未知模型 {name!r}。请使用别名：{choices}，或 provider:model 格式。") from exc
    if not model:
        raise ValueError("provider:model 中的 model 不能为空。")
    return PROVIDERS[provider], model


def get_api_key(config: ProviderConfig) -> str:
    key = os.getenv(config.api_key_env)
    if not key:
        raise ValueError(f"未设置环境变量 {config.api_key_env}。")
    return key
