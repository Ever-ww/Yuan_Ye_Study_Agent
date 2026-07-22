"""使用独立无持久化 Agent 完成 Session 上下文压缩。"""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from Agent.config import RuntimeConfig
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from memory import MemoryStore
from prompt import compose_compression_messages
from tools import AsyncToolRegistry


class CompressionResult(BaseModel):
    """一次压缩的可审计结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    status: Literal["compressed", "fallback", "error"]
    session_id: str
    attempts: int = Field(ge=0, le=3)
    source_file: str
    target_file: str | None = None
    profile_file: str | None = None
    records_processed: int = Field(default=0, ge=0)
    conversation_turns: int = Field(default=0, ge=0)
    message: str = ""

    def payload(self) -> dict[str, Any]:
        """输出 Runtime 事件可直接消费的 Python 字典。"""
        return self.model_dump(mode="python")


class _CompressionOutput(BaseModel):
    """压缩模型必须返回的严格结构。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    profile_markdown: str = Field(min_length=1)
    context_summary_markdown: str = Field(min_length=1)

    @field_validator("profile_markdown", "context_summary_markdown")
    @classmethod
    def _strip_non_empty_markdown(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Markdown 内容不能为空")
        return value


class ContextProcessor:
    """压缩当前分段，并在失败后对模型输入执行非破坏性裁剪。"""

    def __init__(
        self,
        config: RuntimeConfig,
        memory: MemoryStore,
        *,
        provider_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.provider_factory = provider_factory or self._build_provider
        self._fallback_sessions: set[str] = set()

    async def compress(self, session_id: str) -> CompressionResult:
        """最多调用三次压缩 Agent，成功后合并 Profile 并切换分段。"""
        source_file = self.memory.active_filename(session_id)
        records = self.memory.session_records(session_id)
        if not any(record.get("role") in {"user", "assistant", "tool"} for record in records):
            return CompressionResult(
                status="error", session_id=session_id, attempts=0, source_file=source_file,
                message="当前会话没有可压缩内容",
            )
        normalized = _normalize_records(records)
        existing = self.memory.profiles.session_profile(session_id)
        validation_error = ""
        for attempt in range(1, 4):
            try:
                messages = compose_compression_messages(normalized, existing, validation_error)
                raw = await self._run_compression_agent(messages)
                profile, summary = _parse_output(raw)
                turns = sum(1 for record in records if record.get("role") == "user")
                tool_calls = sum(
                    len(record.get("tool_calls", []))
                    for record in records
                    if isinstance(record.get("tool_calls"), list)
                )
                profile_path, segment = self.memory.commit_compression(
                    session_id,
                    profile_markdown=profile,
                    context_summary=summary,
                    source_file=source_file,
                    conversation_turns=turns,
                    records_processed=len(records),
                    tool_calls_processed=tool_calls,
                )
                self._fallback_sessions.discard(session_id)
                return CompressionResult(
                    status="compressed", session_id=session_id, attempts=attempt, source_file=source_file,
                    target_file=segment.name,
                    profile_file=profile_path.name,
                    records_processed=len(records),
                    conversation_turns=turns,
                    message=f"上下文压缩完成：{len(records)} 条记录 → {segment.name}",
                )
            except Exception as exc:
                validation_error = str(exc) or type(exc).__name__
        self._fallback_sessions.add(session_id)
        return CompressionResult(
            status="fallback", session_id=session_id, attempts=3, source_file=source_file,
            records_processed=len(records),
            conversation_turns=sum(1 for record in records if record.get("role") == "user"),
            message=f"压缩连续失败 3 次，已启用内存上下文裁剪：{validation_error}",
        )

    def trim_messages_if_needed(self, session_id: str, messages: list[dict[str, Any]]) -> bool:
        """压缩失败后按最旧完整对话块裁剪本轮内存消息。"""
        threshold = self.config.compression_threshold_tokens
        if session_id not in self._fallback_sessions or threshold <= 0:
            return False
        if _message_tokens(messages) <= threshold:
            return False
        systems: list[dict[str, Any]] = []
        rest = list(messages)
        while rest and rest[0].get("role") == "system":
            systems.append(rest.pop(0))
        current = rest.pop() if rest and rest[-1].get("role") == "user" else None
        blocks = _conversation_blocks(rest)
        changed = False
        while blocks and _message_tokens([*systems, *(item for block in blocks for item in block), *([current] if current else [])]) > threshold:
            blocks.pop(0)
            changed = True
        messages[:] = [*systems, *(item for block in blocks for item in block), *([current] if current else [])]
        return changed

    async def _run_compression_agent(self, messages: list[dict[str, str]]) -> str:
        """创建无工具、无 Memory 回调的临时 AgentRuntime。"""
        from Agent.runtime.engine import AgentRuntime

        hooks = HookRegistry()

        async def inject_prompt(event: HookEvent) -> None:
            event.data["messages"] = [dict(message) for message in messages]
            event.data["tools"] = []

        hooks.register(HookPoint.MODEL_BEFORE, inject_prompt, priority=-100)
        child_config = self.config.model_copy(update={"stream": False, "compression_threshold_tokens": 0})
        runtime = AgentRuntime(
            child_config,
            provider=self.provider_factory(),
            tools=AsyncToolRegistry(),
            hooks=hooks,
            enable_context_processing=False,
            enable_subagent=False,
        )
        result = await runtime.run("压缩当前会话上下文")
        if not result.completed:
            raise RuntimeError("压缩 Agent 未返回完整结果")
        return result.answer

    def _build_provider(self):
        return build_provider(
            self.config.provider,
            self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            stream=False,
        )


def _normalize_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仅保留压缩模型需要理解的对话和工具字段。"""
    normalized = []
    for record in records:
        value = {"role": record.get("role"), "content": record.get("content")}
        for key in ("tool_calls", "tool_call_id", "name", "status"):
            if key in record:
                value[key] = record[key]
        normalized.append(value)
    return normalized


def _parse_output(raw: str) -> tuple[str, str]:
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
        if value.startswith("json"):
            value = value[4:].lstrip()
    output = _CompressionOutput.model_validate_json(value)
    return output.profile_markdown, output.context_summary_markdown


def _conversation_blocks(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按 user 起点划分完整对话块，避免拆散 assistant/tool 链。"""
    blocks: list[list[dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "user" or not blocks:
            blocks.append([])
        blocks[-1].append(message)
    return blocks


def _message_tokens(messages: list[dict[str, Any]]) -> int:
    value = json.dumps(messages, ensure_ascii=False)
    cjk = sum(1 for char in value if "\u3400" <= char <= "\u9fff")
    return cjk + math.ceil((len(value) - cjk) / 4)
