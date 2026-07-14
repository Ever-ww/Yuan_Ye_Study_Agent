"""同步统一聊天客户端，基于 Python 标准库实现。"""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import ProviderConfig, get_api_key, resolve_model
from .exceptions import AuthenticationError, ModelAPIError
from .models import ChatMessage, ChatResponse


class Transport(Protocol):
    def __call__(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


def _http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 -- URL comes from trusted provider config
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise AuthenticationError("API 密钥无效或没有访问权限。") from exc
        raise ModelAPIError(exc.code, body) from exc


class ModelClient:
    """以同一 ``chat`` 方法调用 GPT、Claude、DeepSeek、GLM、Qwen、Kimi。

    ``model`` 支持别名（``gpt``、``claude`` 等）或 ``provider:model``，例如
    ``qwen:qwen3-235b-a22b``。
    """

    def __init__(self, model: str, *, api_key: str | None = None, timeout: float = 60, transport: Transport = _http_post, provider_config: ProviderConfig | None = None) -> None:
        resolved_config, self.model = resolve_model(model)
        self.config = provider_config or resolved_config
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def from_config(cls, model: str | None = None, *, config_path: str | None = None, api_key: str | None = None) -> "ModelClient":
        """从 ``config.ini`` 创建客户端；可传 model 临时覆盖默认模型。"""
        from .settings import load_settings

        settings = load_settings(config_path)
        provider_config, resolved_model = settings.resolve_model(model)
        qualified_model = f"{provider_config.provider.value}:{resolved_model}"
        return cls(
            qualified_model,
            api_key=api_key or settings.api_key_for(provider_config.provider.value),
            timeout=settings.timeout_seconds,
            provider_config=provider_config,
        )

    def chat(self, messages: list[ChatMessage] | list[dict[str, str]], *, temperature: float | None = None, max_tokens: int | None = None) -> ChatResponse:
        normalized = [m if isinstance(m, ChatMessage) else ChatMessage(**m) for m in messages]
        key = self.api_key or get_api_key(self.config)
        try:
            if self.config.api_style == "responses":
                return self._responses(normalized, key, temperature, max_tokens)
            if self.config.api_style == "messages":
                return self._anthropic(normalized, key, temperature, max_tokens)
            return self._chat_completions(normalized, key, temperature, max_tokens)
        except ValueError as exc:
            if "API_KEY" in str(exc):
                raise AuthenticationError(str(exc)) from exc
            raise

    def _post(self, path: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        return self._transport(f"{self.config.base_url}{path}", headers, payload, self.timeout)

    def _responses(self, messages: list[ChatMessage], key: str, temperature: float | None, max_tokens: int | None) -> ChatResponse:
        payload: dict[str, Any] = {"model": self.model, "input": [m.__dict__ for m in messages]}
        if temperature is not None: payload["temperature"] = temperature
        if max_tokens is not None: payload["max_output_tokens"] = max_tokens
        raw = self._post("/responses", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, payload)
        usage = raw.get("usage", {})
        return ChatResponse(raw.get("output_text", ""), raw.get("model", self.model), self.config.provider.value, usage.get("input_tokens"), usage.get("output_tokens"), raw)

    def _anthropic(self, messages: list[ChatMessage], key: str, temperature: float | None, max_tokens: int | None) -> ChatResponse:
        system = "\n".join(m.content for m in messages if m.role == "system") or None
        payload: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens or 1024, "messages": [m.__dict__ for m in messages if m.role != "system"]}
        if system: payload["system"] = system
        if temperature is not None: payload["temperature"] = temperature
        raw = self._post("/messages", {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, payload)
        usage = raw.get("usage", {})
        content = "".join(block.get("text", "") for block in raw.get("content", []) if block.get("type") == "text")
        return ChatResponse(content, raw.get("model", self.model), self.config.provider.value, usage.get("input_tokens"), usage.get("output_tokens"), raw)

    def _chat_completions(self, messages: list[ChatMessage], key: str, temperature: float | None, max_tokens: int | None) -> ChatResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": [m.__dict__ for m in messages]}
        if temperature is not None: payload["temperature"] = temperature
        if max_tokens is not None: payload["max_tokens"] = max_tokens
        raw = self._post("/chat/completions", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, payload)
        usage = raw.get("usage", {})
        content = raw["choices"][0]["message"]["content"]
        return ChatResponse(content, raw.get("model", self.model), self.config.provider.value, usage.get("prompt_tokens"), usage.get("completion_tokens"), raw)
