"""把旧版同步多供应商客户端适配为 Harness 所需的异步模型接口。

运行时优先尝试各供应商的原生工具调用格式，以保留结构化参数和调用 ID；若
兼容网关不支持对应格式，则退回严格 JSON Action 协议。底层 HTTP 客户端仍是
同步实现，因此使用 :func:`asyncio.to_thread` 避免阻塞事件循环。当前 ``stream``
只是兼容异步迭代器接口的“伪流式”：完整响应生成后一次性 yield，并非逐 token
网络流。这个限制在以后接入供应商 SSE 时可在不改变运行时协议的情况下解除。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from model_choice import ModelClient
from model_choice.config import get_api_key, resolve_model
from model_choice.settings import load_settings

from Agent.types import ModelMessage, ModelOutput, ToolCall


class LegacyModelProvider:
    """基于 :class:`ModelClient` 的异步 Harness Provider。

    工具存在时先调用供应商原生 tool-calling；任何原生调用异常都会转入可移植
    JSON 协议。这种回退主要兼容“声称 OpenAI 兼容但不完整支持 tools”的网关，
    但也意味着第一次失败会产生一次额外模型请求。无工具时直接走同步客户端的
    统一文本接口。图片在原生路径按供应商格式发送；JSON 文本回退无法传图，
    会明确插入附件被省略的提示，避免模型误以为看过图片。

    对运行时暴露的接口已经支持结构化 :class:`ToolCall`。真正的供应商级 SSE
    流式可日后替换 ``stream`` 实现，而无需改变 :class:`AgentRuntime`。
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        providers: dict[str, dict[str, str]] | None = None,
        timeout: float = 60,
    ) -> None:
        """创建 Provider，并解析模型、网关覆盖及本机密钥。

        Args:
            model: 模型别名或 ``provider:model``；为 ``None`` 时使用配置默认值。
            providers: Harness 合并后的供应商覆盖，支持 ``base_url``、
                ``api_key_env`` 和仅驻留内存的 ``api_key``。
            timeout: 同步 HTTP 请求超时秒数。

        密钥优先级为：``providers`` 中显式值 > 覆盖后环境变量 > 系统 keyring。
        未传任何 Harness 覆盖时，先复用 ``ModelClient.from_config`` 的旧 INI
        解析结果；若其中没有显式密钥，再尝试 keyring。keyring 是可选依赖，
        未安装时静默跳过，最终由请求阶段报告缺少环境变量。
        """

        overrides = providers or {}
        if model is None and not overrides:
            self.client = ModelClient.from_config()
            if not self.client.api_key:
                try:
                    import keyring
                    self.client.api_key = keyring.get_password("yy-agent", self.client.config.provider.value)
                except ImportError:
                    pass
            return
        selected = model or load_settings().default_model
        provider_config, resolved_model = resolve_model(selected)
        override = overrides.get(provider_config.provider.value, {})
        provider_config = replace(
            provider_config,
            base_url=override.get("base_url", provider_config.base_url).rstrip("/"),
            api_key_env=override.get("api_key_env", provider_config.api_key_env),
        )
        key = override.get("api_key") or os.getenv(provider_config.api_key_env)
        if not key:
            try:
                import keyring
                key = keyring.get_password("yy-agent", provider_config.provider.value)
            except ImportError:
                key = None
        self.client = ModelClient(
            f"{provider_config.provider.value}:{resolved_model}",
            api_key=key or None,
            timeout=timeout,
            provider_config=provider_config,
        )

    async def complete(
        self,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0,
    ) -> ModelOutput:
        """生成一次完整响应，并把文本与工具调用统一为 :class:`ModelOutput`。

        有工具定义时，首先在线程池中执行原生工具调用。兼容性异常会触发严格
        JSON fallback，要求模型只返回一个 action 对象。解析失败不会猜测或执行
        工具，而是保留原始文本作为普通输出；只有 action 名称为非空字符串且
        ``action_input`` 是字典时才构造工具调用。这一保守策略可降低格式漂移
        导致误执行有副作用工具的风险。

        注意：原生路径的广泛异常捕获是协议兼容机制，不代表错误消失；若随后
        JSON 回退也失败，后一次异常仍会传给上层 Provider fallback/运行时。
        """

        if tools:
            try:
                return await asyncio.to_thread(self._native_complete, messages, tools, temperature)
            except Exception:
                # 不同“兼容”网关对 tools 字段的实现差异较大；严格 JSON 协议是
                # 跨供应商的最低共同能力。这里不得复用任何未确认的工具结果。
                pass
        else:
            prepared = [dict(role=m.role, content=m.content + ("\n[Image attachment omitted by text-only fallback]" if m.images else "")) for m in messages]
            response = await asyncio.to_thread(self.client.chat, prepared, temperature=temperature)
            return ModelOutput(
                content=response.content, model=response.model, provider=response.provider,
                input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            )
        protocol = (
            "Return exactly one JSON object. To call a tool: "
            '{"action":"tool-name","action_input":{...}}. '
            'To finish: {"action":"final","final":"answer"}. '
            f"Available tools: {json.dumps(tools, ensure_ascii=False)}"
        )
        prepared = [dict(role=m.role, content=m.content + ("\n[Image attachment omitted by text-only fallback]" if m.images else "")) for m in messages]
        if prepared and prepared[0]["role"] == "system":
            prepared[0]["content"] += "\n\n" + protocol
        else:
            prepared.insert(0, {"role": "system", "content": protocol})
        response = await asyncio.to_thread(self.client.chat, prepared, temperature=temperature)
        decision = parse_json_decision(response.content)
        calls: tuple[ToolCall, ...] = ()
        content = response.content
        action = decision.get("action")
        if action == "final":
            content = str(decision.get("final", ""))
        elif isinstance(action, str) and action:
            arguments = decision.get("action_input", {})
            if isinstance(arguments, dict):
                calls = (ToolCall(uuid4().hex, action, arguments),)
                content = ""
        return ModelOutput(
            content=content,
            tool_calls=calls,
            model=response.model,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    def _native_complete(self, messages: list[ModelMessage], tools: list[dict[str, Any]], temperature: float) -> ModelOutput:
        """按 API 风格发送原生工具定义并解析结构化工具调用。

        三条分支的关键差异如下：

        * Anthropic Messages 使用顶层 ``system``、``input_schema`` 与
          ``tool_use`` 内容块；
        * OpenAI Responses 使用 ``input``、扁平 function 工具定义，以及
          ``function_call`` 输出项；
        * Chat Completions 把函数定义放在 ``tools[].function``，调用则位于
          ``choices[0].message.tool_calls``。

        字符串形式的 arguments 只做 JSON 解码，不做 ``eval`` 或容错补全；因此
        恶意文本不会在模型层执行，最终权限检查仍由 Harness 工具层负责。
        """

        key = self.client.api_key or get_api_key(self.client.config)
        style = self.client.config.api_style
        if style == "messages":
            system = "\n".join(item.content for item in messages if item.role == "system")
            payload: dict[str, Any] = {
                "model": self.client.model,
                "max_tokens": 4096,
                "messages": [self._anthropic_message(item) for item in messages if item.role != "system"],
                "tools": [{"name": item["name"], "description": item["description"], "input_schema": item["parameters"]} for item in tools],
                "temperature": temperature,
            }
            if system:
                payload["system"] = system
            raw = self.client._post("/messages", {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, payload)
            calls = tuple(
                ToolCall(str(block.get("id") or uuid4().hex), str(block["name"]), dict(block.get("input", {})))
                for block in raw.get("content", []) if block.get("type") == "tool_use"
            )
            content = "".join(str(block.get("text", "")) for block in raw.get("content", []) if block.get("type") == "text")
            usage = raw.get("usage", {})
            return ModelOutput(content, calls, raw.get("model", self.client.model), self.client.config.provider.value, usage.get("input_tokens"), usage.get("output_tokens"))
        if style == "responses":
            payload = {
                "model": self.client.model,
                "input": [self._responses_message(item) for item in messages],
                "tools": [{"type": "function", "name": item["name"], "description": item["description"], "parameters": item["parameters"]} for item in tools],
                "temperature": temperature,
            }
            raw = self.client._post("/responses", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, payload)
            calls_list, text_parts = [], []
            for item in raw.get("output", []):
                if item.get("type") == "function_call":
                    arguments = item.get("arguments", {})
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    calls_list.append(ToolCall(str(item.get("call_id") or item.get("id") or uuid4().hex), str(item["name"]), dict(arguments)))
                for block in item.get("content", []):
                    if block.get("type") in {"output_text", "text"}:
                        text_parts.append(str(block.get("text", "")))
            usage = raw.get("usage", {})
            return ModelOutput("".join(text_parts) or str(raw.get("output_text", "")), tuple(calls_list), raw.get("model", self.client.model), self.client.config.provider.value, usage.get("input_tokens"), usage.get("output_tokens"))
        payload = {
            "model": self.client.model,
            "messages": [self._chat_message(item) for item in messages],
            "tools": [{"type": "function", "function": {"name": item["name"], "description": item["description"], "parameters": item["parameters"]}} for item in tools],
            "temperature": temperature,
        }
        raw = self.client._post("/chat/completions", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, payload)
        message = raw["choices"][0]["message"]
        calls_list = []
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            calls_list.append(ToolCall(str(call.get("id") or uuid4().hex), str(function["name"]), dict(arguments)))
        usage = raw.get("usage", {})
        return ModelOutput(str(message.get("content") or ""), tuple(calls_list), raw.get("model", self.client.model), self.client.config.provider.value, usage.get("prompt_tokens"), usage.get("completion_tokens"))

    @staticmethod
    def _anthropic_message(message: ModelMessage) -> dict[str, Any]:
        """将统一消息转换为 Anthropic 文本/内联 base64 图片内容块。"""

        if not message.images:
            return {"role": message.role, "content": message.content}
        content: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
        content.extend({"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.data_base64}} for image in message.images)
        return {"role": message.role, "content": content}

    @staticmethod
    def _responses_message(message: ModelMessage) -> dict[str, Any]:
        """将统一消息转换为 Responses API 的 input_text/input_image 块。"""

        if not message.images:
            return {"role": message.role, "content": message.content}
        content: list[dict[str, Any]] = [{"type": "input_text", "text": message.content}]
        content.extend({"type": "input_image", "image_url": f"data:{image.media_type};base64,{image.data_base64}"} for image in message.images)
        return {"role": message.role, "content": content}

    @staticmethod
    def _chat_message(message: ModelMessage) -> dict[str, Any]:
        """将统一消息转换为 Chat Completions 多模态 content 数组。"""

        if not message.images:
            return {"role": message.role, "content": message.content}
        content: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
        content.extend({"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{image.data_base64}"}} for image in message.images)
        return {"role": message.role, "content": content}

    async def stream(
        self,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0,
    ) -> AsyncIterator[str]:
        """以异步迭代器形式返回响应文本。

        当前先等待 :meth:`complete` 完成，再至多 yield 一个文本片段，属于接口
        兼容的伪流式。工具调用只存在于 ``complete`` 返回值中，不会混入文本流。
        """

        output = await self.complete(messages, tools, temperature=temperature)
        if output.content:
            yield output.content


class FallbackModelProvider:
    """在主 Provider 失败时切换到一个可选备用 Provider。

    切换发生在单次模型生成边界，而不是重跑整个 Agent turn。已完成的工具调用
    及其副作用由运行时事件状态保存，不会因为模型切换而自动再次执行。
    """

    def __init__(self, primary: Any, fallback: Any | None = None) -> None:
        """保存主、备 Provider；``fallback=None`` 表示只传播主模型异常。"""

        self.primary = primary
        self.fallback = fallback

    async def complete(self, messages: list[ModelMessage], tools: list[dict[str, Any]], *, temperature: float = 0) -> ModelOutput:
        """优先调用主模型，失败且配置备用模型时仅重试本次生成。"""

        try:
            return await self.primary.complete(messages, tools, temperature=temperature)
        except Exception:
            if self.fallback is None:
                raise
            return await self.fallback.complete(messages, tools, temperature=temperature)

    async def stream(self, messages: list[ModelMessage], tools: list[dict[str, Any]], *, temperature: float = 0) -> AsyncIterator[str]:
        """提供与主 Provider 相同的伪流式接口，实际回退由 ``complete`` 处理。"""

        output = await self.complete(messages, tools, temperature=temperature)
        if output.content:
            yield output.content


def parse_json_decision(text: str) -> dict[str, Any]:
    """保守解析 JSON Action 协议的单个对象。

    接受纯 JSON 或完整包裹在 Markdown JSON 代码围栏中的 JSON。不会从自然语言
    中搜索子串、修复尾逗号或执行 Python 表达式；任何解码失败和非对象顶层值
    都返回空字典。调用者据此把不可信格式当普通文本，而不是冒险执行工具。
    """

    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL)
    if fenced:
        value = fenced.group(1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
