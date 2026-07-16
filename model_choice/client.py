"""使用统一同步接口访问不同供应商的文本聊天 API。

实现刻意只依赖 Python 标准库，既能供旧版同步 ReAct Agent 使用，也能被新的
异步 Provider 通过 :func:`asyncio.to_thread` 包装。供应商之间的端点、认证头、
system 消息位置及 token 字段并不一致，本模块将这些差异收敛为
:class:`ChatResponse`。这里的 ``chat`` 是一次性完整响应，不提供网络级流式传输。
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import ProviderConfig, get_api_key, resolve_model
from .exceptions import AuthenticationError, ModelAPIError
from .models import ChatMessage, ChatResponse


class Transport(Protocol):
    """可注入的 JSON HTTP 传输协议。

    把传输层抽象为可调用对象，测试可以传入不联网的假实现，企业部署也可以
    注入审计代理。实现者应返回已解码的 JSON 字典，并在失败时抛出异常。
    """

    def __call__(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """向 ``url`` 发送 JSON POST，并返回供应商响应。"""
        ...


def _http_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """用标准库执行一次同步 JSON POST 请求。

    ``url`` 只能由受信任的 :class:`ProviderConfig` 及固定端点拼接得到；调用者
    不应把用户输入直接作为 URL。401/403 被归一化为认证错误，其他 HTTP 错误
    保留状态码与响应正文，便于上层诊断限流或参数错误。连接失败、超时以及
    非 JSON 响应会沿用标准库异常，交由 Provider 的回退策略处理。
    """

    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        # S310 在这里可安全忽略：URL 只来自受信任的供应商配置，而不是模型或工具的任意输入。
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise AuthenticationError("API 密钥无效或没有访问权限。") from exc
        raise ModelAPIError(exc.code, body) from exc


class ModelClient:
    """以同一 ``chat`` 方法调用 GPT、Claude、DeepSeek、GLM、Qwen、Kimi。

    ``model`` 支持别名（``gpt``、``claude`` 等）或 ``provider:model``，例如
    ``qwen:qwen3-235b-a22b``。客户端在构造时完成模型路由，调用 ``chat`` 时再
    根据配置中的 ``api_style`` 选择 Responses、Messages 或 Chat Completions
    适配器。

    密钥解析优先级为：构造函数显式 ``api_key`` > 配置文件传入的密钥 > 对应
    环境变量。异步 Harness 还可在构造客户端前从系统凭据库取值。密钥不会被
    放入返回的数据模型或异常文本。
    """

    def __init__(self, model: str, *, api_key: str | None = None, timeout: float = 60, transport: Transport = _http_post, provider_config: ProviderConfig | None = None) -> None:
        """创建绑定到单一模型和供应商的客户端。

        Args:
            model: 模型别名或 ``provider:model`` 限定名。
            api_key: 可选的显式密钥，优先于环境变量。
            timeout: 单次同步 HTTP 请求的超时秒数。
            transport: 可替换传输函数，主要用于测试与受控网络环境。
            provider_config: 可选配置副本，用于覆盖网关地址等部署参数；模型名
                仍由 ``model`` 解析，避免覆盖项改变供应商语义。
        """

        resolved_config, self.model = resolve_model(model)
        self.config = provider_config or resolved_config
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def from_config(cls, model: str | None = None, *, config_path: str | None = None, api_key: str | None = None) -> "ModelClient":
        """从 ``config.ini`` 创建客户端，并应用供应商覆盖配置。

        ``model`` 只覆盖本次实例的默认模型，不写回配置文件；``api_key`` 又优先
        于配置文件中的兼容密钥字段。推荐部署仍使用环境变量或本机凭据库，避免
        将秘密写入仓库中的 INI 文件。
        """
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
        """发送一次非流式聊天请求并返回供应商无关的响应。

        字典消息会先严格转换为 :class:`ChatMessage`，从而尽早发现角色或字段
        错误。随后按 ``api_style`` 路由到对应私有适配器。仅将“缺少 API_KEY”
        这类配置错误转换为 :class:`AuthenticationError`，其他 ``ValueError``
        原样抛出，以免掩盖响应解析或调用参数问题。
        """

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
        """将固定 API 路径拼到根地址并交给可注入传输层。"""

        return self._transport(f"{self.config.base_url}{path}", headers, payload, self.timeout)

    def _responses(self, messages: list[ChatMessage], key: str, temperature: float | None, max_tokens: int | None) -> ChatResponse:
        """适配 OpenAI Responses API 的请求与 token 字段。

        Responses 使用 ``input``、``max_output_tokens`` 与顶层 ``output_text``；
        这与 Chat Completions 的 messages/choices 结构不能混用。
        """

        payload: dict[str, Any] = {"model": self.model, "input": [m.__dict__ for m in messages]}
        if temperature is not None: payload["temperature"] = temperature
        if max_tokens is not None: payload["max_output_tokens"] = max_tokens
        raw = self._post("/responses", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, payload)
        usage = raw.get("usage", {})
        return ChatResponse(raw.get("output_text", ""), raw.get("model", self.model), self.config.provider.value, usage.get("input_tokens"), usage.get("output_tokens"), raw)

    def _anthropic(self, messages: list[ChatMessage], key: str, temperature: float | None, max_tokens: int | None) -> ChatResponse:
        """适配 Anthropic Messages API。

        Anthropic 将 system 指令放在顶层而非 ``messages`` 中，因此这里会合并
        所有 system 消息，并只从 ``content`` 中的文本块构造统一响应。该同步
        客户端当前只处理文本块；工具块由异步 Provider 的原生调用路径解析。
        """

        system = "\n".join(m.content for m in messages if m.role == "system") or None
        payload: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens or 1024, "messages": [m.__dict__ for m in messages if m.role != "system"]}
        if system: payload["system"] = system
        if temperature is not None: payload["temperature"] = temperature
        raw = self._post("/messages", {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, payload)
        usage = raw.get("usage", {})
        content = "".join(block.get("text", "") for block in raw.get("content", []) if block.get("type") == "text")
        return ChatResponse(content, raw.get("model", self.model), self.config.provider.value, usage.get("input_tokens"), usage.get("output_tokens"), raw)

    def _chat_completions(self, messages: list[ChatMessage], key: str, temperature: float | None, max_tokens: int | None) -> ChatResponse:
        """适配 OpenAI 兼容的 Chat Completions API。

        DeepSeek、GLM、Qwen 和 Kimi 复用这一请求形状，但仍使用各自的根地址与
        环境变量。响应 token 名为 ``prompt_tokens``/``completion_tokens``，在
        返回时映射到统一字段。
        """

        payload: dict[str, Any] = {"model": self.model, "messages": [m.__dict__ for m in messages]}
        if temperature is not None: payload["temperature"] = temperature
        if max_tokens is not None: payload["max_tokens"] = max_tokens
        raw = self._post("/chat/completions", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, payload)
        usage = raw.get("usage", {})
        content = raw["choices"][0]["message"]["content"]
        return ChatResponse(content, raw.get("model", self.model), self.config.provider.value, usage.get("prompt_tokens"), usage.get("completion_tokens"), raw)
