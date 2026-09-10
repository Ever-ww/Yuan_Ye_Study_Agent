"""会话 JSONL 与长期 Profile 的统一记忆门面。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .profile import ProfileStore
from .persistence import SessionPersistenceProjection
from .session import SessionStore
from .structured import (
    LegacyMemoryMigrator,
    MemoryIndexWorker,
    MemoryProfileProjector,
    MemoryWriter,
    StructuredMemoryStore,
)
from .retrieval import MemoryRetriever, project_identity
from .long_term import MemoryTurnSnapshot


class MemoryStore:
    """项目 `.yy/memory` 下全部记忆能力的唯一入口。"""

    def __init__(
        self,
        root: Path,
        *,
        workspace_root: Path | None = None,
        agent_root: Path | None = None,
        partition_by_workspace: bool = True,
        profiles: ProfileStore | None = None,
        memory_identity_root: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.agent_root = (agent_root or _infer_agent_root(self.root)).resolve()
        self.workspace_root = (workspace_root or self.agent_root).resolve()
        self.memory_identity_root = (memory_identity_root or self.workspace_root).resolve()
        session_directory = self.root / "session"
        self.partition_by_workspace = partition_by_workspace
        if partition_by_workspace and self.workspace_root != self.agent_root:
            session_directory /= _workspace_key(self.workspace_root)
        self.sessions = SessionStore(session_directory)
        self.profiles = profiles or ProfileStore(self.root / "profile")
        if self.profiles.directory.resolve() != (self.root / "profile").resolve():
            raise ValueError("ProfileStore 必须位于 MemoryStore 的 profile 目录")
        self.session_profiles_enabled = self.profiles.session_profiles_enabled
        self._message_cache: dict[str, list[dict[str, Any]]] = {}
        self.memory_degraded_reason = ""
        try:
            self.structured = StructuredMemoryStore(self.root)
            self.memory_writer = MemoryWriter(self.structured)
            self.memory_index_worker = MemoryIndexWorker(self.structured)
            self.memory_profile_projector = MemoryProfileProjector(self.structured)
            self.memory_retriever = MemoryRetriever(self.structured)
            self.memory_migrator = LegacyMemoryMigrator(
                self.structured,
                project_key=project_identity(str(self.memory_identity_root)),
            )
            self.memory_migrator.migrate()
        except (sqlite3.Error, OSError, ValueError) as exc:
            # Conversation persistence remains available even when the optional
            # long-term store cannot be opened. Writes fail closed; recall is empty.
            self.memory_degraded_reason = f"{type(exc).__name__}: {str(exc)[:500]}"
            self.structured = None
            self.memory_writer = _UnavailableMemoryWriter(self.memory_degraded_reason)
            self.memory_index_worker = _NoopMemoryWorker()
            self.memory_profile_projector = _NoopMemoryWorker()
            self.memory_retriever = _UnavailableMemoryRetriever(self.memory_degraded_reason)
            self.memory_migrator = None
        self.initialize()

    def initialize(self) -> None:
        """确保首次运行所需目录、索引和默认 Profile 全部存在。"""
        self.sessions.initialize()
        self.profiles.initialize()
        # Projection work is bounded and idempotent. It is safe for every
        # Runtime construction to help drain a small amount of durable work;
        # Gateway-owned workers can drain the remainder later.
        self.memory_index_worker.reconcile(limit=100)
        self.memory_profile_projector.reconcile(limit=100)

    def memory_health(self) -> dict[str, object]:
        if self.structured is None:
            return {
                "memory_record_count": 0,
                "memory_index_pending": 0,
                "memory_index_failed": 0,
                "semantic_retrieval_available": False,
                "memory_degraded": True,
                "memory_degradation_reason": self.memory_degraded_reason,
            }
        value = self.structured.health()
        value["retrieval_audit_failures"] = self.memory_retriever.retrieval_audit_failures
        value["semantic_retrieval_available"] = (
            self.memory_retriever.embedding_provider is not None
        )
        return value

    def configure_long_term_retrieval(self, config) -> None:
        """Attach only the explicitly configured Memory embedding provider."""
        if self.structured is None:
            return
        from .embeddings import build_memory_embedding_provider

        provider = build_memory_embedding_provider(config)
        self.memory_retriever.configure_semantic(
            provider,
            version=int(getattr(config, "memory_embedding_version", 1)),
        )

    def create_session(self, first_message: str, session_id: str | None = None) -> str:
        """创建会话并返回稳定哈希。"""
        SessionPersistenceProjection.assert_persistable(first_message)
        return self.sessions.create(first_message, session_id)

    def record_user(
        self,
        session_id: str,
        content: str,
        *,
        origin: Literal["interactive", "cron", "maintenance"] = "interactive",
        audit: dict[str, object] | None = None,
        provider_context: dict[str, object] | None = None,
    ) -> str:
        """记录一条用户输入。"""
        SessionPersistenceProjection.assert_persistable(content)
        cache = self._ensure_cache(session_id)
        metadata = {"origin": origin, **(audit or {})}
        if provider_context is not None:
            metadata["provider_context"] = provider_context
        record_id = self.sessions.append(session_id, "user", content, metadata)
        cache.append({"role": "user", "content": content})
        return record_id

    def record_extension_annotation(
        self, session_id: str, content: str, *, hook_id: str,
        source_hash: str, run_id: str | None = None,
    ) -> str:
        """Persist an auditable annotation that is never restored into model context."""
        SessionPersistenceProjection.assert_persistable(content)
        return self.sessions.append(
            session_id,
            "extension",
            content,
            {
                "origin": "extension",
                "hook_id": hook_id,
                "source_hash": source_hash,
                "run_id": run_id,
            },
        )

    def record_assistant(
        self,
        session_id: str,
        content: str,
        *,
        model: dict[str, object] | None = None,
        model_calls: list[dict[str, object]] | None = None,
        task_latency_ms: float | None = None,
        reasoning: str | None = None,
        audit: dict[str, object] | None = None,
    ) -> str:
        """记录最终助手回复，以及本次用户任务的模型、时延和 Token 指标。"""
        content = SessionPersistenceProjection.strip_ephemeral(content)
        metadata: dict[str, object] = {}
        if model is not None:
            metadata["model"] = model
        if model_calls is not None:
            metadata["model_calls"] = model_calls
        if task_latency_ms is not None:
            metadata["task_latency_ms"] = task_latency_ms
        if reasoning:
            metadata["reasoning"] = reasoning
        metadata.update(audit or {})
        cache = self._ensure_cache(session_id)
        record_id = self.sessions.append(session_id, "assistant", content, metadata)
        cache.append({"role": "assistant", "content": content})
        return record_id

    def record_model_tool_calls(
        self,
        session_id: str,
        *,
        content: str | None,
        tool_calls: list[dict[str, Any]],
        model: dict[str, object],
        model_call: dict[str, object],
        reasoning: str | None = None,
        audit: dict[str, object] | None = None,
    ) -> str:
        """记录模型原始返回的标准 assistant.tool_calls 消息。"""
        SessionPersistenceProjection.assert_no_ephemeral(tool_calls)
        if content is not None:
            content = SessionPersistenceProjection.strip_ephemeral(content)
        metadata: dict[str, object] = {
            "tool_calls": tool_calls,
            "model": model,
            "model_call": model_call,
        }
        if reasoning:
            metadata["reasoning"] = reasoning
        metadata.update(audit or {})
        cache = self._ensure_cache(session_id)
        record_id = self.sessions.append(session_id, "assistant", content, metadata)
        cache.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        return record_id

    def record_tool_result(
        self,
        session_id: str,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        status: str,
        arguments: dict[str, Any],
        record_id: str | None = None,
        audit: dict[str, object] | None = None,
    ) -> str:
        """记录工具成功结果或错误反馈。"""
        SessionPersistenceProjection.assert_no_ephemeral(arguments)
        content = SessionPersistenceProjection.strip_ephemeral(content)
        cache = self._ensure_cache(session_id)
        selected_record_id = record_id or uuid4().hex
        inserted = self.sessions.append_once(session_id, {
            "role": "tool",
            "content": content,
            "record_id": selected_record_id,
            "tool_call_id": tool_call_id,
            "name": name,
            "status": status,
            "arguments": arguments,
            **(audit or {}),
        })
        if inserted or not any(
            item.get("record_id") == selected_record_id
            or (
                item.get("role") == "tool"
                and item.get("tool_call_id") == tool_call_id
                and item.get("name") == name
                and item.get("content") == content
            )
            for item in cache
        ):
            cache.append({
                "role": "tool", "tool_call_id": tool_call_id, "name": name,
                "content": content, "record_id": selected_record_id,
            })
        return selected_record_id

    def record_cancellation(
        self,
        session_id: str,
        *,
        audit: dict[str, object] | None = None,
    ) -> str | None:
        """补齐未完成工具链并记录用户取消，保证后续消息角色合法。"""
        cache = self._ensure_cache(session_id)
        if not cache:
            return None
        last = cache[-1]
        if last.get("role") == "assistant" and not last.get("tool_calls"):
            return None

        pending: list[tuple[str, str]] = []
        assistant_index = next((
            index for index in range(len(cache) - 1, -1, -1)
            if cache[index].get("role") == "assistant" and cache[index].get("tool_calls")
        ), None)
        if assistant_index is not None:
            calls = cache[assistant_index].get("tool_calls")
            completed = {
                str(message.get("tool_call_id"))
                for message in cache[assistant_index + 1 :]
                if message.get("role") == "tool"
            }
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "")
                    function = call.get("function")
                    name = str(function.get("name") or "") if isinstance(function, dict) else ""
                    if call_id and name and call_id not in completed:
                        pending.append((call_id, name))
        for call_id, name in pending:
            synthetic_audit = {
                **(audit or {}),
                "record_id": self._stable_terminal_record_id(
                    session_id, "tool_cancelled", call_id, audit,
                ),
            }
            self.record_tool_result(
                session_id,
                tool_call_id=call_id,
                name=name,
                content="工具执行已由用户按 Ctrl+C 终止",
                status="cancelled",
                arguments={},
                audit=synthetic_audit,
            )

        return self._record_terminal_marker(
            session_id,
            "本次回答已由用户按 Ctrl+C 终止。",
            status="cancelled",
            audit=audit,
        )

    def record_network_failure(
        self,
        session_id: str,
        *,
        audit: dict[str, object] | None = None,
    ) -> str | None:
        """网络重试耗尽时闭合当前问答，允许用户在同一 Session 重新发送。"""
        return self._record_terminal_marker(
            session_id,
            "本次回答因网络连接中断未完成，请重新发送问题。",
            status="network_error",
            audit=audit,
        )

    def record_turn_failure(
        self,
        session_id: str,
        message: str,
        *,
        audit: dict[str, object] | None = None,
    ) -> str | None:
        """闭合本轮尚未执行的并行工具调用，并保存可继续恢复的失败标记。"""
        cache = self._ensure_cache(session_id)
        pending = self._pending_tool_calls(cache)
        for call_id, name in pending:
            synthetic_audit = {
                **(audit or {}),
                "record_id": self._stable_terminal_record_id(
                    session_id, "tool_skipped", call_id, audit,
                ),
            }
            self.record_tool_result(
                session_id,
                tool_call_id=call_id,
                name=name,
                content="同一批次的前序工具执行失败，本工具未执行。",
                status="skipped",
                arguments={},
                audit=synthetic_audit,
            )
        detail = (message or "运行时错误").strip()
        return self._record_terminal_marker(
            session_id,
            f"本次回答因运行错误未完成：{detail}",
            status="error",
            audit=audit,
        )

    def record_cancelled_partial(
        self,
        session_id: str,
        content: str,
        *,
        audit: dict[str, object] | None = None,
    ) -> str | None:
        """保存 Ctrl+C 前已流式生成的文本，不接纳未完成的工具调用字段。"""
        if not content:
            return None
        cache = self._ensure_cache(session_id)
        if not cache or cache[-1].get("role") not in {"user", "tool"}:
            return None
        metadata = {
            "status": "cancelled",
            **(audit or {}),
            "record_id": self._stable_terminal_record_id(
                session_id, "cancelled_partial", None, audit,
            ),
        }
        record_id = self.sessions.append(session_id, "assistant", content, metadata)
        cache.append({"role": "assistant", "content": content})
        return record_id

    def restore_messages(self, session_id: str, *, provider_context: bool = False) -> list[dict[str, Any]]:
        """恢复索引指向的最新会话分段。"""
        messages = (
            self.sessions.restore(session_id, provider_context=True) if provider_context
            else [dict(message) for message in self._ensure_cache(session_id)]
        )
        # record_id is Session evidence, not a Provider message field. The preview
        # embeds the exact reference in content without changing request shape
        # across cache restoration or Gateway restart.
        for message in messages:
            message.pop("record_id", None)
        return messages

    def refresh_messages(self, session_id: str) -> list[dict[str, Any]]:
        """显式从最新 JSONL 重建内存消息缓存。"""
        self._message_cache[session_id] = self.sessions.restore(session_id)
        return self.restore_messages(session_id)

    def has_session(self, session_id: str) -> bool:
        """判断会话哈希是否可恢复。"""
        return self.sessions.exists(session_id)

    def list_sessions(self) -> list[dict[str, object]]:
        """返回供 CLI 展示的会话摘要。"""
        return self.sessions.list_sessions()

    def session_records(self, session_id: str) -> list[dict[str, object]]:
        """读取带时间戳的原始会话记录。"""
        return self.sessions.read_records(session_id)

    def session_context_records(self, session_id: str) -> list[dict[str, object]]:
        """读取当前摘要及其Hash校验的受保护尾部，供下一次压缩使用。"""
        return self.sessions.context_records(session_id)

    def session_context_records_with_locations(
        self,
        session_id: str,
    ) -> list[dict[str, object]]:
        """Return visible records annotated with their exact Session filename.

        ``session_file`` is a transient recovery selector.  It is never written
        back into canonical Session JSONL and is added only when a record ID maps
        to exactly one indexed file.
        """
        locations: dict[str, list[str]] = {}
        for filename, record in self.sessions.read_all_records_strict(session_id):
            if record.record_id:
                locations.setdefault(record.record_id, []).append(filename)
        selected: list[dict[str, object]] = []
        for record in self.sessions.context_records(session_id):
            value = dict(record)
            record_id = value.get("record_id")
            matches = locations.get(str(record_id), []) if record_id else []
            if len(matches) == 1:
                value["session_file"] = matches[0]
            selected.append(value)
        return selected

    def protected_tail_refs(
        self,
        session_id: str,
        records: list[dict[str, object]],
    ) -> list[dict[str, str]]:
        return self.sessions.make_record_refs(session_id, records)

    def profile_context(self, session_id: str | None = None) -> str:
        """返回全局 Profile 与指定会话独占的哈希 Profile。"""
        return self.profiles.load_for_session(session_id)

    def prompt_context(self, session_id: str | None = None) -> str:
        """返回注入模型的预算内长期上下文；普通用户记忆维持 6000 字符上限。"""
        value = self.profile_context(session_id)
        limit = self.profiles.prompt_context_limit
        return value if limit is None else value[:limit]

    def has_compressible_history(self, session_id: str) -> bool:
        """判断当前分段是否包含可被摘要的对话或工具记录。"""
        return any(
            record.get("role") in {"user", "assistant", "tool"}
            for record in self.session_context_records(session_id)
        )

    def active_filename(self, session_id: str) -> str:
        """返回会话当前 JSONL 文件名。"""
        return self.sessions.active_filename(session_id)

    def active_path(self, session_id: str) -> Path:
        return self.sessions.active_path(session_id)

    def session_created_at(self, session_id: str) -> str:
        return self.sessions.created_at(session_id)

    def session_skill_catalog(self, session_id: str) -> dict[str, object] | None:
        return self.sessions.skill_catalog(session_id)

    def set_session_skill_catalog(self, session_id: str, catalog: dict[str, object]) -> None:
        self.sessions.set_skill_catalog(session_id, catalog)

    def latest_summary(self, session_id: str) -> str:
        import json

        for record in self.sessions.read_records(session_id):
            if record.get("role") == "summary" and isinstance(record.get("content"), str):
                sources = sorted({
                    str(ref["segment"])
                    for key in ("summary_source_refs", "summary_history_refs")
                    for ref in record.get(key, [])
                    if isinstance(ref, dict) and ref.get("segment")
                } | ({str(record["source_file"])} if record.get("source_file") else set())
                  | {name for name in record.get("summary_original_segments", []) if isinstance(name, str)})
                return str(record["content"]) + (
                    "\n\n原始对话来源（当前 Session；可用 session_history 按 segment/query 回查）："
                    + json.dumps(sources, ensure_ascii=False) if sources else ""
                )
        return ""

    def invalidate_session_cache(self, session_id: str) -> None:
        self._message_cache.pop(session_id, None)

    def rollover_with_summary(
        self,
        session_id: str,
        summary: str,
        source_file: str,
        *,
        metadata: dict[str, object] | None = None,
        skill_catalog: dict[str, object] | None = None,
    ) -> Path:
        """创建以 summary 记录开头的新会话分段。"""
        record: dict[str, object] = {
            "role": "summary",
            "content": summary,
            "source_file": source_file,
        }
        if metadata:
            record.update(metadata)
        result = self.sessions.rollover(
            session_id,
            [record],
            skill_catalog=skill_catalog,
        )
        if self.structured is not None:
            try:
                from .long_term import MemoryScope, MemoryWriteRequest

                self.memory_writer.write(MemoryWriteRequest(
                    scope=MemoryScope.SESSION,
                    scope_key=session_id,
                    kind="summary",
                    content=summary,
                    source="compression",
                    source_ref=(
                        f"compression:{session_id}:{source_file}:"
                        + hashlib.sha256(summary.encode("utf-8")).hexdigest()
                    ),
                    locator=source_file,
                    confidence=0.9,
                    importance=0.6,
                ))
            except Exception as exc:
                self.memory_degraded_reason = (
                    f"compression_memory_projection:{type(exc).__name__}: {str(exc)[:300]}"
                )
        self.refresh_messages(session_id)
        return result

    def commit_compression(
        self,
        session_id: str,
        *,
        profile_markdown: str,
        context_summary: str,
        source_file: str,
        conversation_turns: int,
        records_processed: int,
        tool_calls_processed: int,
        summary_metadata: dict[str, object] | None = None,
        skill_catalog: dict[str, object] | None = None,
    ) -> tuple[Path | None, Path]:
        """协调 Profile 与新分段写入；切段失败时恢复旧 Profile 状态。"""
        # Compatibility input only: cumulative Profile Markdown is no longer a
        # Memory authority. Structured candidates must go through MemoryWriter.
        del profile_markdown, conversation_turns, records_processed, tool_calls_processed
        return None, self.rollover_with_summary(
            session_id,
            context_summary,
            source_file,
            metadata=summary_metadata,
            skill_catalog=skill_catalog,
        )

    def _ensure_cache(self, session_id: str) -> list[dict[str, Any]]:
        if session_id not in self._message_cache:
            self._message_cache[session_id] = self.sessions.restore(session_id)
        return self._message_cache[session_id]

    def _record_terminal_marker(
        self,
        session_id: str,
        content: str,
        *,
        status: str,
        audit: dict[str, object] | None = None,
    ) -> str | None:
        cache = self._ensure_cache(session_id)
        if not cache or cache[-1].get("role") not in {"user", "tool"}:
            return None
        metadata = {
            "status": status,
            **(audit or {}),
            "record_id": self._stable_terminal_record_id(
                session_id, f"terminal_{status}", None, audit,
            ),
        }
        record_id = self.sessions.append(session_id, "assistant", content, metadata)
        cache.append({"role": "assistant", "content": content})
        return record_id

    @staticmethod
    def _stable_terminal_record_id(
        session_id: str,
        kind: str,
        suffix: str | None,
        audit: dict[str, object] | None,
    ) -> str:
        selected = audit or {}
        identity = ":".join((
            session_id,
            str(selected.get("run_id") or "legacy"),
            str(selected.get("turn_id") or "legacy"),
            kind,
            suffix or "",
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _pending_tool_calls(cache: list[dict[str, Any]]) -> list[tuple[str, str]]:
        assistant_index = next((
            index for index in range(len(cache) - 1, -1, -1)
            if cache[index].get("role") == "assistant" and cache[index].get("tool_calls")
        ), None)
        if assistant_index is None:
            return []
        completed = {
            str(message.get("tool_call_id"))
            for message in cache[assistant_index + 1 :]
            if message.get("role") == "tool"
        }
        pending: list[tuple[str, str]] = []
        calls = cache[assistant_index].get("tool_calls")
        if not isinstance(calls, list):
            return pending
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            function = call.get("function")
            name = str(function.get("name") or "") if isinstance(function, dict) else ""
            if call_id and name and call_id not in completed:
                pending.append((call_id, name))
        return pending


def _infer_agent_root(memory_root: Path) -> Path:
    if memory_root.name == "memory" and memory_root.parent.name == ".yy":
        return memory_root.parents[1]
    return memory_root


def _workspace_key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class _UnavailableMemoryWriter:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def write(self, request):
        del request
        raise RuntimeError(f"canonical long-term Memory is unavailable: {self.reason}")


class _NoopMemoryWorker:
    def reconcile(self, *, limit: int = 100) -> int:
        del limit
        return 0


class _UnavailableMemoryRetriever:
    retrieval_audit_failures = 0

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def retrieve(self, query, access, *, session_id: str, run_id=None, turn_id=None):
        return MemoryTurnSnapshot(
            snapshot_id="mrs_degraded_" + hashlib.sha256(
                f"{session_id}:{self.reason}".encode()
            ).hexdigest()[:16],
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            store_watermark=0,
            query_hash=hashlib.sha256(str(query).encode()).hexdigest(),
            token_budget=access.profile.token_budget,
            used_tokens=0,
            degradation_reason="canonical_store_unavailable",
            created_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        )
