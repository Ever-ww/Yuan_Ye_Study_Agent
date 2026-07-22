"""异步模型适配器；所有供应商归一为 ModelProvider。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from Agent.contracts import ModelReply, TokenUsage, ToolCall
from .errors import ModelNetworkError, ModelResponseFormatError, ModelServiceError


def _response_excerpt(value: object, limit: int = 12000) -> str:
    """生成不包含请求凭据的有限响应片段，供本机故障复现。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return text[:limit]


def _content_text(value: object) -> str:
    """兼容字符串、空内容和常见文本块数组。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(str(block["text"]))
            else:
                raise ModelResponseFormatError("模型 content 包含无法识别的文本块", _response_excerpt(value))
        return "".join(parts)
    raise ModelResponseFormatError("模型 content 必须是字符串、数组或 null", _response_excerpt(value))


def _tool_arguments(value: object, response: object) -> dict[str, Any]:
    """把 OpenAI-compatible 的字符串或对象参数归一为字典。"""
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ModelResponseFormatError("模型返回了无效的工具参数 JSON", _response_excerpt(response)) from exc
        if isinstance(parsed, dict):
            return parsed
    raise ModelResponseFormatError("模型工具参数必须是 JSON 对象", _response_excerpt(response))


def _openai_reply(data: object) -> ModelReply:
    """把非流式 OpenAI-compatible 响应严格转换为 ModelReply。"""
    try:
        if not isinstance(data, dict):
            raise TypeError("响应不是对象")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise TypeError("choices 缺失")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise TypeError("message 缺失")
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise TypeError("tool_calls 不是数组")
        calls: list[ToolCall] = []
        for item in raw_calls:
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                raise TypeError("tool_calls.function 缺失")
            function = item["function"]
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise TypeError("工具名称缺失")
            call_id = item.get("id")
            calls.append(ToolCall(
                name=name,
                arguments=_tool_arguments(function.get("arguments"), data),
                id=str(call_id) if call_id else None,
            ))
        return ModelReply(
            text=_content_text(message.get("content")),
            tool_calls=tuple(calls),
            finished=not calls,
            usage=_openai_usage(data.get("usage")),
        )
    except ModelResponseFormatError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelResponseFormatError(f"无法解析模型响应：{exc}", _response_excerpt(data)) from exc


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
            raise ModelServiceError(
                f"模型服务返回 HTTP {exc.response.status_code}；请检查 model、base_url 与 api_key",
                exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelNetworkError(f"模型网络请求失败（{type(exc).__name__}）；请检查网络和 base_url") from exc
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModelResponseFormatError("模型服务返回的正文不是合法 JSON", response.text[:12000]) from exc
        return _openai_reply(data)

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
                        try:
                            packet = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise ModelResponseFormatError("模型返回了无效的 SSE JSON", data[:12000]) from exc
                        if not isinstance(packet, dict):
                            raise ModelResponseFormatError("模型 SSE 数据必须是 JSON 对象", _response_excerpt(packet))
                        packet_usage = _openai_usage(packet.get("usage"))
                        if packet_usage is not None:
                            usage = packet_usage
                        choices = packet.get("choices", [])
                        if not choices:
                            continue
                        if not isinstance(choices[0], dict):
                            raise ModelResponseFormatError("模型 SSE choices 元素必须是对象", _response_excerpt(packet))
                        delta = choices[0].get("delta", {})
                        if not isinstance(delta, dict):
                            raise ModelResponseFormatError("模型 SSE delta 必须是对象", _response_excerpt(packet))
                        content = delta.get("content")
                        if content:
                            yield ModelReply(text=str(content), finished=False)
                        raw_tool_calls = delta.get("tool_calls", [])
                        if not isinstance(raw_tool_calls, list):
                            raise ModelResponseFormatError("模型 SSE tool_calls 必须是数组", _response_excerpt(packet))
                        for item in raw_tool_calls:
                            if not isinstance(item, dict):
                                raise ModelResponseFormatError("模型 SSE tool_call 必须是对象", _response_excerpt(packet))
                            try:
                                index = int(item.get("index", 0))
                            except (TypeError, ValueError) as exc:
                                raise ModelResponseFormatError("模型 SSE 工具索引必须是整数", _response_excerpt(packet)) from exc
                            slot = pending_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            slot["id"] += str(item.get("id") or "")
                            function = item.get("function", {})
                            if not isinstance(function, dict):
                                raise ModelResponseFormatError("模型 SSE function 必须是对象", _response_excerpt(packet))
                            slot["name"] += str(function.get("name") or "")
                            arguments = function.get("arguments", "")
                            if not isinstance(arguments, str):
                                raise ModelResponseFormatError("模型 SSE 增量参数必须是字符串", _response_excerpt(packet))
                            slot["arguments"] += arguments
        except httpx.HTTPStatusError as exc:
            raise ModelServiceError(
                f"模型服务返回 HTTP {exc.response.status_code}；请检查 model、base_url 与 api_key",
                exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelNetworkError(f"模型流式网络请求失败（{type(exc).__name__}）；请检查网络、代理和 base_url") from exc
        calls: list[ToolCall] = []
        for slot in pending_calls.values():
            if not slot["name"]:
                raise ModelResponseFormatError("模型流式工具调用缺少名称", _response_excerpt(pending_calls))
            try:
                calls.append(ToolCall(
                    name=slot["name"],
                    arguments=json.loads(slot["arguments"] or "{}"),
                    id=slot["id"] or None,
                ))
            except json.JSONDecodeError as exc:
                raise ModelResponseFormatError(
                    f"模型返回了无效的流式工具参数：{slot['name']}",
                    _response_excerpt(pending_calls),
                ) from exc
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
            raise ModelServiceError(
                f"模型服务返回 HTTP {exc.response.status_code}；请检查 model、base_url 与 api_key",
                exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelNetworkError(f"模型网络请求失败（{type(exc).__name__}）；请检查网络和 base_url") from exc
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModelResponseFormatError("Anthropic 服务返回的正文不是合法 JSON", response.text[:12000]) from exc
        if not isinstance(data, dict) or not isinstance(data.get("content", []), list):
            raise ModelResponseFormatError("Anthropic 响应必须包含 content 数组", _response_excerpt(data))
        blocks = data.get("content", [])
        if any(not isinstance(item, dict) for item in blocks):
            raise ModelResponseFormatError("Anthropic content 元素必须是对象", _response_excerpt(data))
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            raw_usage = {}
        usage = TokenUsage(
            input_tokens=int(raw_usage["input_tokens"]) if isinstance(raw_usage.get("input_tokens"), (int, float)) else None,
            output_tokens=int(raw_usage["output_tokens"]) if isinstance(raw_usage.get("output_tokens"), (int, float)) else None,
        )
        text_parts: list[str] = []
        for item in blocks:
            if item.get("type") != "text":
                continue
            text = item.get("text", "")
            if not isinstance(text, str):
                raise ModelResponseFormatError("Anthropic 文本块的 text 必须是字符串", _response_excerpt(data))
            text_parts.append(text)
        return ModelReply(text="".join(text_parts), usage=usage)

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
