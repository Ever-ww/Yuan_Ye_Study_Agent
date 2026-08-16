"""使用独立无持久化 Agent 完成 Session 上下文压缩。"""

from __future__ import annotations

import json
import math
import hashlib
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from Agent.config import RuntimeConfig
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from memory import MemoryStore
from prompt import compose_compression_messages
from tool import AsyncToolRegistry
from .budget import (
    ContextBudgetController,
    ContextBudgetEstimate,
    ContextBudgetExceeded,
    ContextCompressionPolicy,
    canonical_json,
    estimate_tokens,
)


class CompressionResult(BaseModel):
    """一次压缩的可审计结果。"""

    model_config = ConfigDict(frozen=True, strict=True)

    status: Literal["compressed", "fallback", "error"]
    session_id: str
    attempts: int = Field(ge=0, le=4)
    source_file: str
    target_file: str | None = None
    profile_file: str | None = None
    records_processed: int = Field(default=0, ge=0)
    conversation_turns: int = Field(default=0, ge=0)
    messages_reloaded: bool = False
    protected_tail_messages: int = Field(default=0, ge=0)
    projected_tool_output_tokens: int = Field(default=0, ge=0)
    compression_model: str | None = None
    compression_fallback_reason: str | None = None
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


class _SummaryOnlyOutput(BaseModel):
    """禁用 Session Profile 时压缩模型只允许返回上下文摘要。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    context_summary_markdown: str = Field(min_length=1)

    @field_validator("context_summary_markdown")
    @classmethod
    def _strip_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("上下文摘要不能为空")
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
        self.provider_factory = provider_factory
        self.budget = ContextBudgetController(config)
        self._fallback_sessions: set[str] = set()
        self._last_pressure: dict[str, ContextBudgetEstimate] = {}

    async def compress(
        self,
        session_id: str,
        *,
        summary_metadata: dict[str, object] | None = None,
        skill_catalog: dict[str, object] | None = None,
    ) -> CompressionResult:
        """最多调用三次压缩 Agent，成功后合并 Profile 并切换分段。"""
        return await self.compress_with_policy(
            session_id,
            summary_metadata=summary_metadata,
            skill_catalog=skill_catalog,
            reason="manual",
            protect_recent=False,
        )

    async def compress_with_policy(
        self,
        session_id: str,
        *,
        current_query: str | None = None,
        summary_metadata: dict[str, object] | None = None,
        skill_catalog: dict[str, object] | None = None,
        reason: str = "projected_total",
        protect_recent: bool = True,
    ) -> CompressionResult:
        """Compress old complete blocks while carrying recent canonical records by hash reference."""
        source_file = self.memory.active_filename(session_id)
        records = self.memory.session_context_records(session_id)
        if not any(record.get("role") in {"user", "assistant", "tool"} for record in records):
            return CompressionResult(
                status="error", session_id=session_id, attempts=0, source_file=source_file,
                message="当前会话没有可压缩内容",
            )
        compressible, protected = _select_compression_records(
            records,
            self.budget.policy,
            current_query=current_query,
            protect_recent=protect_recent,
        )
        if not any(record.get("role") in {"user", "assistant", "tool"} for record in compressible):
            return CompressionResult(
                status="error",
                session_id=session_id,
                attempts=0,
                source_file=source_file,
                protected_tail_messages=len(protected),
                message="当前请求只有受保护的近期上下文，没有可安全压缩的完整历史块",
            )
        normalized = _normalize_records(compressible)
        include_profile = self.memory.session_profiles_enabled
        existing = self.memory.profiles.session_profile(session_id) if include_profile else ""
        validation_error = ""
        input_tokens = estimate_tokens(canonical_json(normalized))
        providers, provider_fallback_reason = self._compression_provider_candidates(input_tokens)
        attempts = 0
        selected_model: str | None = None
        for provider_factory, provider_name, allowed_attempts in providers:
            for _ in range(allowed_attempts):
                attempts += 1
                selected_model = provider_name
                try:
                    messages = compose_compression_messages(
                        normalized,
                        existing,
                        validation_error,
                        include_profile=include_profile,
                    )
                    raw = await self._run_compression_agent(messages, provider_factory())
                    profile, summary = _parse_output(raw, include_profile=include_profile)
                    turns = sum(1 for record in compressible if record.get("role") == "user")
                    tool_calls = sum(
                        len(record.get("tool_calls", []))
                        for record in compressible
                        if isinstance(record.get("tool_calls"), list)
                    )
                    metadata = dict(summary_metadata or {})
                    current_query_record = next((
                        record for record in protected
                        if record.get("role") == "user" and record.get("content") == current_query
                    ), None)
                    metadata.update({
                        "compression_reason": reason,
                        "protected_tail_refs": self.memory.protected_tail_refs(
                            session_id, protected,
                        ),
                    })
                    if isinstance(current_query_record, dict) and isinstance(
                        current_query_record.get("record_id"), str,
                    ):
                        metadata["protected_current_query_record_id"] = current_query_record["record_id"]
                    profile_path, segment = self.memory.commit_compression(
                        session_id,
                        profile_markdown=profile,
                        context_summary=summary,
                        source_file=source_file,
                        conversation_turns=turns,
                        records_processed=len(compressible),
                        tool_calls_processed=tool_calls,
                        summary_metadata=metadata,
                        skill_catalog=skill_catalog,
                    )
                    self._fallback_sessions.discard(session_id)
                    return CompressionResult(
                        status="compressed", session_id=session_id, attempts=attempts, source_file=source_file,
                        target_file=segment.name,
                        profile_file=profile_path.name if profile_path is not None else None,
                        records_processed=len(compressible),
                        conversation_turns=turns,
                        protected_tail_messages=len(protected),
                        compression_model=selected_model,
                        compression_fallback_reason=provider_fallback_reason,
                        message=(
                            f"上下文压缩完成：{len(compressible)} 条记录 → {segment.name}；"
                            f"保留近期 {len(protected)} 条"
                        ),
                    )
                except Exception as exc:
                    validation_error = str(exc) or type(exc).__name__
                    if provider_name.startswith("auxiliary:"):
                        provider_fallback_reason = f"auxiliary_unavailable:{type(exc).__name__}"
        self._fallback_sessions.add(session_id)
        return CompressionResult(
            status="fallback", session_id=session_id, attempts=attempts, source_file=source_file,
            records_processed=len(compressible),
            conversation_turns=sum(1 for record in compressible if record.get("role") == "user"),
            protected_tail_messages=len(protected),
            compression_model=selected_model,
            compression_fallback_reason=provider_fallback_reason or validation_error,
            message=f"压缩连续失败 3 次，已启用内存上下文裁剪：{validation_error}",
        )

    async def prepare_before_model(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        reload_messages: Callable[[], list[dict[str, Any]]] | None = None,
        ephemeral_preview: str | None = None,
        current_query: str | None = None,
        reason: str | None = None,
    ) -> CompressionResult | None:
        """在真实请求前估算完整上下文，超限时压缩并替换当前内存消息。"""
        estimate = self.forecast(
            session_id, messages, tools, ephemeral_preview=ephemeral_preview,
        )
        if self.config.compression_threshold_tokens <= 0 or estimate.decision == "proceed":
            return None
        if session_id in self._fallback_sessions:
            self.trim_messages_if_needed(session_id, messages)
            return None
        if not self.memory.has_compressible_history(session_id):
            return None
        result = await self.compress_with_policy(
            session_id,
            current_query=current_query,
            reason=reason or estimate.reason,
        )
        if result.status == "compressed" and reload_messages is not None:
            messages[:] = reload_messages()
            return result.model_copy(update={"messages_reloaded": True})
        if result.status == "fallback":
            self.trim_messages_if_needed(session_id, messages)
        return result

    def should_compress(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> bool:
        """判断当前请求是否具备自动压缩条件，不产生任何持久化副作用。"""
        estimate = self.forecast(session_id, messages, tools)
        return (
            self.config.compression_threshold_tokens > 0
            and session_id not in self._fallback_sessions
            and estimate.decision in {"compress", "reject"}
            and self.memory.has_compressible_history(session_id)
        )

    def forecast(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        ephemeral_preview: str | None = None,
    ) -> ContextBudgetEstimate:
        estimate = self.budget.forecast(
            session_id, messages, tools, ephemeral_preview=ephemeral_preview,
        )
        self._last_pressure[session_id] = estimate
        return estimate

    def update_from_provider_usage(
        self,
        session_id: str,
        *,
        estimated_input_tokens: int,
        provider_input_tokens: int | None,
    ):
        return self.budget.update_from_provider_usage(
            session_id,
            estimated_input_tokens=estimated_input_tokens,
            provider_input_tokens=provider_input_tokens,
        )

    def finalize_request(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ContextBudgetEstimate:
        """Apply transient large-tool projection, then enforce the hard provider boundary."""
        first = self.forecast(session_id, messages, tools)
        projected = 0
        if first.decision in {"compress", "reject"}:
            projected = _project_large_tool_outputs(
                messages,
                max_chars=self.config.tool_output_max_chars,
            )
        result = self.forecast(session_id, messages, tools)
        if projected:
            result = result.model_copy(update={"projected_tool_output_tokens": projected})
            self._last_pressure[session_id] = result
        if result.projected_total_tokens >= result.hard_limit_tokens:
            raise ContextBudgetExceeded(
                "当前Query、稳定Prompt、Tool Schema与必要近期上下文超过模型硬限制，无法安全裁剪",
                estimate=result,
            )
        return result

    async def recover_from_overflow(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        current_query: str,
        reload_messages: Callable[[], list[dict[str, Any]]] | None,
    ) -> CompressionResult:
        """Perform the single emergency compression allowed for an explicit provider rejection."""
        result = await self.compress_with_policy(
            session_id,
            current_query=current_query,
            reason="provider_context_rejection",
            protect_recent=False,
        )
        if result.status == "compressed" and reload_messages is not None:
            messages[:] = reload_messages()
            result = result.model_copy(update={"messages_reloaded": True})
        else:
            self._fallback_sessions.add(session_id)
            changed = self.trim_messages_if_needed(session_id, messages, force=True)
            if changed and result.status == "error":
                result = result.model_copy(
                    update={
                        "status": "fallback",
                        "message": "Provider拒绝上下文后，已仅为本次请求裁剪最旧完整对话块",
                        "compression_fallback_reason": "provider_context_rejection",
                    },
                )
        self.finalize_request(session_id, messages, tools)
        return result

    def fallback_active(self, session_id: str) -> bool:
        """返回当前进程内该 Session 是否已进入压缩失败裁剪模式。"""
        return session_id in self._fallback_sessions

    def discard_fallback(self, session_id: str) -> None:
        """供未提交任何上下文变更的维护事务撤销进程内降级标记。"""
        self._fallback_sessions.discard(session_id)

    def trim_messages_if_needed(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> bool:
        """压缩失败后按最旧完整对话块裁剪本轮内存消息。"""
        threshold = self.budget.policy.trigger_limit_tokens - self.budget.policy.output_reserve_tokens
        threshold = max(1, threshold)
        if (session_id not in self._fallback_sessions and not force) or self.config.compression_threshold_tokens <= 0:
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

    async def _run_compression_agent(self, messages: list[dict[str, str]], provider: Any) -> str:
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
            provider=provider,
            tools=AsyncToolRegistry(),
            hooks=hooks,
            enable_context_processing=False,
            enable_skills=False,
            enable_subagent=False,
            enable_sandbox=False,
            enable_extensions=False,
            enable_references=False,
            raise_errors=True,
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
            use_system_proxy=self.config.use_system_proxy,
            proxy_url=self.config.proxy_url,
        )

    def _build_auxiliary_provider(self):
        return build_provider(
            self.config.compression_provider or self.config.provider,
            self.config.compression_model or self.config.model,
            base_url=self.config.compression_base_url or self.config.base_url,
            api_key=self.config.compression_api_key or self.config.api_key,
            stream=False,
            use_system_proxy=self.config.use_system_proxy,
            proxy_url=self.config.proxy_url,
        )

    def _compression_provider_candidates(
        self,
        input_tokens: int,
    ) -> tuple[list[tuple[Callable[[], Any], str, int]], str | None]:
        if self.provider_factory is not None:
            return [(self.provider_factory, "custom", 3)], None
        selected: list[tuple[Callable[[], Any], str, int]] = []
        fallback_reason: str | None = None
        auxiliary_configured = any((
            self.config.compression_provider,
            self.config.compression_model,
            self.config.compression_base_url,
            self.config.compression_api_key,
        ))
        if auxiliary_configured:
            window = self.config.compression_context_window_tokens or self.config.model_context_window_tokens
            if input_tokens + self.config.compression_output_reserve_tokens < window:
                selected.append((
                    self._build_auxiliary_provider,
                    f"auxiliary:{self.config.compression_provider or self.config.provider}/"
                    f"{self.config.compression_model or self.config.model}",
                    1,
                ))
            else:
                fallback_reason = "auxiliary_context_window_insufficient"
        selected.append((self._build_provider, f"main:{self.config.provider}/{self.config.model}", 3))
        return selected, fallback_reason


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


def _parse_output(raw: str, *, include_profile: bool = True) -> tuple[str, str]:
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
        if value.startswith("json"):
            value = value[4:].lstrip()
    if include_profile:
        output = _CompressionOutput.model_validate_json(value)
        return output.profile_markdown, output.context_summary_markdown
    output = _SummaryOnlyOutput.model_validate_json(value)
    return "", output.context_summary_markdown


def _conversation_blocks(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按 user 起点划分完整对话块，避免拆散 assistant/tool 链。"""
    blocks: list[list[dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "user" or not blocks:
            blocks.append([])
        blocks[-1].append(message)
    return blocks


def _select_compression_records(
    records: list[dict[str, Any]],
    policy: ContextCompressionPolicy,
    *,
    current_query: str | None,
    protect_recent: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries = [record for record in records if record.get("role") == "summary"]
    conversational = [record for record in records if record.get("role") in {"user", "assistant", "tool"}]
    blocks = _conversation_blocks(conversational)
    protected_blocks: list[list[dict[str, Any]]] = []
    protected_count = 0
    tail_budget = max(0, int(policy.trigger_limit_tokens * policy.target_ratio))
    used_tokens = 0
    mandatory_records: list[dict[str, Any]] = []
    if current_query is not None and blocks:
        first_user = next((item for item in blocks[-1] if item.get("role") == "user"), None)
        if first_user is not None and first_user.get("content") == current_query:
            # The current query is immutable provider input. A completed
            # assistant/tool suffix may be summarized, but the query itself is
            # always carried into the new segment by canonical record reference.
            mandatory_records = [first_user]
    if not protect_recent:
        mandatory_ids = {id(record) for record in mandatory_records}
        return (
            [*summaries, *(record for record in conversational if id(record) not in mandatory_ids)],
            mandatory_records,
        )
    protected_count = len(mandatory_records)
    for block in reversed(blocks):
        if mandatory_records and block is blocks[-1]:
            continue
        block_count = len(block)
        block_tokens = _message_tokens(block)
        if (
            protected_count + block_count > policy.protect_last_n
            or used_tokens + block_tokens > tail_budget
        ):
            break
        if block_tokens + used_tokens <= tail_budget:
            protected_blocks.append(block)
            protected_count += block_count
            used_tokens += block_tokens
    protected_ids = {
        id(record)
        for record in [*mandatory_records, *(item for block in protected_blocks for item in block)]
    }
    protected = [record for record in conversational if id(record) in protected_ids]
    compressible = [*summaries, *(record for record in conversational if id(record) not in protected_ids)]
    return compressible, protected


def _project_large_tool_outputs(messages: list[dict[str, Any]], *, max_chars: int) -> int:
    """Trim completed historical tool results in the provider projection only."""
    if max_chars <= 0:
        return 0
    projected_tokens = 0
    cutoff = max(0, len(messages) - 3)
    for index, message in enumerate(messages):
        content = message.get("content")
        if index >= cutoff or message.get("role") != "tool" or not isinstance(content, str):
            continue
        if len(content) <= max_chars or "[历史工具输出投影" in content:
            continue
        head = max_chars // 2
        tail = max_chars - head
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        message["content"] = (
            f"{content[:head]}\n\n[历史工具输出投影：sha256={digest}，原始字符={len(content)}]"
            f"\n\n{content[-tail:] if tail else ''}"
        )
        projected_tokens += estimate_tokens(content) - estimate_tokens(str(message["content"]))
    return max(0, projected_tokens)


def _message_tokens(messages: list[dict[str, Any]]) -> int:
    value = json.dumps(messages, ensure_ascii=False)
    cjk = sum(1 for char in value if "\u3400" <= char <= "\u9fff")
    return cjk + math.ceil((len(value) - cjk) / 4)


def _request_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    """估算真正发送给 Provider 的消息与工具 Schema 总 Token。"""
    return _message_tokens([{"messages": messages, "tools": tools}])
