"""声明模型供应商元数据，并把用户可读的模型名称解析为确定配置。

本模块只保存不含凭据的静态信息，例如 API 根地址、密钥环境变量名和默认
模型。它不发起网络请求，也不会从配置文件读取密钥，因此可以安全地被 CLI、
配置迁移器和运行时共同导入。模型名称统一在这里解析，可避免同步客户端和
异步 Harness 对同一个别名做出不同解释。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class Provider(str, Enum):
    """Yuan Ye Agent 内建支持的模型供应商标识。

    同时继承 :class:`str` 使枚举值可直接写入 JSON、SQLite 和配置文件；成员值
    也是 ``provider:model`` 语法中 ``provider`` 部分的唯一合法取值。
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    ZHIPU = "zhipu"
    QWEN = "qwen"
    KIMI = "kimi"


@dataclass(frozen=True)
class ProviderConfig:
    """一次模型请求所需的供应商级静态配置。

    Attributes:
        provider: 供应商的规范化枚举值。
        base_url: 不带末尾斜杠的 API 根地址；具体端点由客户端按 API 风格追加。
        api_key_env: 默认读取密钥的环境变量名，而不是密钥本身。
        default_model: 用户只指定供应商时使用的模型名。
        api_style: 请求及响应结构，当前为 ``responses``、``messages`` 或
            ``chat_completions``。三者分别对应 OpenAI Responses、Anthropic
            Messages 以及常见的 OpenAI 兼容 Chat Completions 协议。

    配置被冻结，目的是防止一个客户端的临时覆盖意外污染其他会话。需要覆盖
    地址或环境变量名时，应使用 :func:`dataclasses.replace` 创建副本。
    """

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
    """解析模型别名或完整限定名。

    ``qwen`` 一类简写会命中 :data:`MODEL_ALIASES`，得到项目验证过的默认模型；
    ``qwen:qwen3-235b-a22b`` 则允许调用者显式选择模型。返回值同时包含供应商
    配置和去掉供应商前缀后的模型名，后续代码据此选择 API 路由。

    名称会先去除首尾空白并转为小写。这适合当前供应商的模型命名约定，但若
    将来接入区分大小写的私有网关，需要在这里调整规则，而不应在各客户端中
    分散处理。

    Raises:
        ValueError: 名称既不是已知别名，也不是合法的 ``provider:model``，或
            model 部分为空。
    """
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
    """从供应商指定的环境变量读取 API 密钥。

    该函数是同步客户端的最终凭据来源。显式传给客户端或从本机凭据库取得的
    密钥会在调用本函数之前被优先使用，因此这里不会覆盖更高优先级的选择。
    错误消息仅包含环境变量名，绝不回显密钥内容。

    Raises:
        ValueError: 对应环境变量缺失或为空。
    """

    key = os.getenv(config.api_key_env)
    if not key:
        raise ValueError(f"未设置环境变量 {config.api_key_env}。")
    return key
