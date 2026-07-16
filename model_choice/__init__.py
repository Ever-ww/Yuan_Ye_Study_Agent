"""模型选择包的稳定公共入口。

同步数据模型和客户端可直接导入；异步 Provider 使用模块级惰性属性加载，避免
``Agent.runtime -> model_choice.provider -> Agent.types`` 在包初始化阶段形成循环
导入。调用者仍可使用 ``from model_choice import LegacyModelProvider``，公开 API
不因内部模块拆分而改变。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .client import ModelClient
from .config import Provider, resolve_model
from .exceptions import AuthenticationError, ModelAPIError, ModelChoiceError
from .models import ChatMessage, ChatResponse
from .settings import AppSettings, load_settings

_PROVIDER_EXPORTS = {
    "FallbackModelProvider": (".provider", "FallbackModelProvider"),
    "LegacyModelProvider": (".provider", "LegacyModelProvider"),
}

__all__ = [
    "AuthenticationError",
    "AppSettings",
    "ChatMessage",
    "ChatResponse",
    "FallbackModelProvider",
    "LegacyModelProvider",
    "ModelAPIError",
    "ModelChoiceError",
    "ModelClient",
    "Provider",
    "resolve_model",
    "load_settings",
]


def __getattr__(name: str) -> Any:
    """按需导入异步 Provider，并将结果缓存到模块全局命名空间。

    只有 :data:`_PROVIDER_EXPORTS` 白名单中的名称可被解析；其他名称遵循标准
    模块行为抛出 :class:`AttributeError`。缓存使后续访问不再重复导入。
    """

    target = _PROVIDER_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
