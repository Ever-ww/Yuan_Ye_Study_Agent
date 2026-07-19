"""异步模型适配器；所有供应商归一为 ModelProvider。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from Agent.contracts import ModelReply, TokenUsage, ToolCall


def _openai_usage(value: object) -> TokenUsage | None:
    """兼容 Chat Completions 常见的新旧 usage 字段名。"""
    if not isinstance(value, dict):
        return None
    input_tokens = value.get("prompt_tokens", value.get("input_tokens"))
    output_tokens = value.get("completion_tokens", value.get("output_tokens"))
    return TokenUsage(
        input_tokens=int(input_tokens) if isinstance(input_tokens, (int, float)) else None,
        output_tokens=int(output_tokens) if isinstance(output_tokens, (int, float)) else None,
    )


class EchoProvider:
    """无凭据开发 Provider，用于诊断与自动化测试。"""

    streaming = False

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        """回显最近一条用户消息，保证入口可离线运行。"""
        task = next((str(item["content"]) for item in reversed(messages) if item["role"] == "user"), "")
        return ModelReply(text=f"已收到：{task}")

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[ModelReply]:
        """以统一流接口返回离线回显结果。"""
        yield await self.complete(messages, tools)


class OpenAICompatibleProvider:
    """适用于 OpenAI-compatible Chat Completions 的最小原生工具调用适配器。"""

    def __init__(self, base_url: str, model: str, api_key: str, *, streaming: bool = False) -> None:
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.streaming = streaming

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        """请求供应商并转换其标准 tool_calls 响应。"""
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"模型服务返回 HTTP {exc.response.status_code}；请检查 model、base_url 与 api_key") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"模型网络请求失败（{type(exc).__name__}）；请检查网络和 base_url") from exc
        data = response.json()
        message = data["choices"][0]["message"]
        calls = tuple(ToolCall(item["function"]["name"], json.loads(item["function"]["arguments"]), item.get("id")) for item in message.get("tool_calls", []))
        return ModelReply(text=message.get("content") or "", tool_calls=calls, finished=not calls, usage=_openai_usage(data.get("usage")))

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[ModelReply]:
        """读取 OpenAI-compatible SSE，逐段产出文本并在结束时组装工具调用。"""
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
        pending_calls: dict[int, dict[str, str]] = {}
        usage: TokenUsage | None = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15)) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        packet = json.loads(data)
                        packet_usage = _openai_usage(packet.get("usage"))
                        if packet_usage is not None:
                            usage = packet_usage
                        choices = packet.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield ModelReply(text=str(content), finished=False)
                        for item in delta.get("tool_calls", []):
                            slot = pending_calls.setdefault(int(item.get("index", 0)), {"id": "", "name": "", "arguments": ""})
                            slot["id"] += item.get("id", "")
                            function = item.get("function", {})
                            slot["name"] += function.get("name", "")
                            slot["arguments"] += function.get("arguments", "")
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"模型服务返回 HTTP {exc.response.status_code}；请检查 model、base_url 与 api_key") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"模型流式网络请求失败（{type(exc).__name__}）；请检查网络、代理和 base_url") from exc
        calls: list[ToolCall] = []
        for slot in pending_calls.values():
            try:
                calls.append(ToolCall(slot["name"], json.loads(slot["arguments"] or "{}"), slot["id"] or None))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"模型返回了无效的流式工具参数：{slot['name']}") from exc
        yield ModelReply(tool_calls=tuple(calls), finished=True, usage=usage)


class AnthropicProvider:
    """Anthropic Messages API 的原生异步文本适配器。"""

    def __init__(self, base_url: str, model: str, api_key: str, *, streaming: bool = False) -> None:
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.streaming = streaming

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        """调用 Messages API；首期仅接收其文本最终输出。"""
        system = "\n".join(str(item["content"]) for item in messages if item["role"] == "system")
        conversation = [{"role": item["role"], "content": item["content"]} for item in messages if item["role"] != "system"]
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(f"{self.base_url}/messages", headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, json={"model": self.model, "max_tokens": 2048, "system": system, "messages": conversation})
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"模型服务返回 HTTP {exc.response.status_code}；请检查 model、base_url 与 api_key") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"模型网络请求失败（{type(exc).__name__}）；请检查网络和 base_url") from exc
        data = response.json()
        blocks = data.get("content", [])
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            raw_usage = {}
        usage = TokenUsage(
            input_tokens=int(raw_usage["input_tokens"]) if isinstance(raw_usage.get("input_tokens"), (int, float)) else None,
            output_tokens=int(raw_usage["output_tokens"]) if isinstance(raw_usage.get("output_tokens"), (int, float)) else None,
        )
        return ModelReply(text="".join(item.get("text", "") for item in blocks if item.get("type") == "text"), usage=usage)

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[ModelReply]:
        """在 Anthropic 流式适配完成前，保持统一接口并回退完整响应。"""
        yield await self.complete(messages, tools)


def build_provider(provider: str, model: str, *, base_url: str | None = None, api_key: str | None = None, stream: bool = False) -> EchoProvider | OpenAICompatibleProvider | AnthropicProvider:
    """根据配置构造 Provider；未配置凭据时明确使用离线 Provider。"""
    if provider == "echo":
        return EchoProvider()
    environment = {"openai": "OPENAI_API_KEY", "gpt": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "qwen": "DASHSCOPE_API_KEY", "glm": "ZHIPU_API_KEY", "kimi": "MOONSHOT_API_KEY"}
    key = api_key or os.getenv(environment.get(provider, f"{provider.upper()}_API_KEY"))
    if provider == "anthropic":
        if not key:
            raise ValueError("未配置 Provider anthropic 的 API Key")
        return AnthropicProvider(base_url or "https://api.anthropic.com/v1", model, key, streaming=stream)
    url = base_url or {"openai": "https://api.openai.com/v1", "gpt": "https://api.openai.com/v1", "deepseek": "https://api.deepseek.com/v1", "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1", "glm": "https://open.bigmodel.cn/api/paas/v4", "kimi": "https://api.moonshot.cn/v1"}.get(provider)
    if not key or not url:
        raise ValueError(f"未配置 Provider {provider} 的地址或 API Key")
    return OpenAICompatibleProvider(url, model, key, streaming=stream)
