"""定义旧版同步客户端使用的供应商无关数据模型。

这些轻量数据类是 API 边界：调用者无需知道 Responses、Messages 或 Chat
Completions 的原始字段差异。新的异步 Harness 使用 ``Agent.types`` 中支持图片、
工具调用和事件的数据类型，再由 Provider 转换到本模块的文本消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ChatMessage:
    """一条不可变的纯文本聊天消息。

    ``role`` 仅接受同步接口共同支持的 system/user/assistant；工具结果等扩展角色
    应由异步 Provider 转换后再传入。冻结数据类可防止请求构造过程中意外修改
    调用者的历史消息。
    """

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ChatResponse:
    """归一化后的完整模型响应。

    ``input_tokens`` 与 ``output_tokens`` 在供应商未返回用量时为 ``None``，调用者
    不应把缺失误当成零。``raw`` 保留原始 JSON 以便调试和审计，但可能很大，
    持久化前应遵循会话存储的大小与敏感信息策略。
    """

    content: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict | None = None
