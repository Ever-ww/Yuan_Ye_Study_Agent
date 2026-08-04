"""会话 JSONL 与长期 Profile 的统一记忆门面。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Literal

from .profile import ProfileStore
from .session import SessionStore


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
    ) -> None:
        self.root = root.resolve()
        self.agent_root = (agent_root or _infer_agent_root(self.root)).resolve()
        self.workspace_root = (workspace_root or self.agent_root).resolve()
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
        self.initialize()

    def initialize(self) -> None:
        """确保首次运行所需目录、索引和默认 Profile 全部存在。"""
        self.sessions.initialize()
        self.profiles.initialize()

    def create_session(self, first_message: str, session_id: str | None = None) -> str:
        """创建会话并返回稳定哈希。"""
        return self.sessions.create(first_message, session_id)

    def record_user(
        self,
        session_id: str,
        content: str,
        *,
        origin: Literal["interactive", "cron", "maintenance"] = "interactive",
    ) -> None:
        """记录一条用户输入。"""
        cache = self._ensure_cache(session_id)
        self.sessions.append(session_id, "user", content, {"origin": origin})
        cache.append({"role": "user", "content": content})

    def record_assistant(
        self,
        session_id: str,
        content: str,
        *,
        model: dict[str, object] | None = None,
        model_calls: list[dict[str, object]] | None = None,
        task_latency_ms: float | None = None,
        reasoning: str | None = None,
    ) -> None:
        """记录最终助手回复，以及本次用户任务的模型、时延和 Token 指标。"""
        metadata: dict[str, object] = {}
        if model is not None:
            metadata["model"] = model
        if model_calls is not None:
            metadata["model_calls"] = model_calls
        if task_latency_ms is not None:
            metadata["task_latency_ms"] = task_latency_ms
        if reasoning:
            metadata["reasoning"] = reasoning
        cache = self._ensure_cache(session_id)
        self.sessions.append(session_id, "assistant", content, metadata)
        cache.append({"role": "assistant", "content": content})

    def record_model_tool_calls(
        self,
        session_id: str,
        *,
        content: str | None,
        tool_calls: list[dict[str, Any]],
        model: dict[str, object],
        model_call: dict[str, object],
        reasoning: str | None = None,
    ) -> None:
        """记录模型原始返回的标准 assistant.tool_calls 消息。"""
        metadata: dict[str, object] = {
            "tool_calls": tool_calls,
            "model": model,
            "model_call": model_call,
        }
        if reasoning:
            metadata["reasoning"] = reasoning
        cache = self._ensure_cache(session_id)
        self.sessions.append(session_id, "assistant", content, metadata)
        cache.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

    def record_tool_result(
        self,
        session_id: str,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        status: str,
        arguments: dict[str, Any],
    ) -> None:
        """记录工具成功结果或错误反馈。"""
        cache = self._ensure_cache(session_id)
        self.sessions.append(session_id, "tool", content, {
            "tool_call_id": tool_call_id,
            "name": name,
            "status": status,
            "arguments": arguments,
        })
        cache.append({
            "role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content,
        })

    def record_cancellation(self, session_id: str) -> bool:
        """补齐未完成工具链并记录用户取消，保证后续消息角色合法。"""
        cache = self._ensure_cache(session_id)
        if not cache:
            return False
        last = cache[-1]
        if last.get("role") == "assistant" and not last.get("tool_calls"):
            return False

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
            self.record_tool_result(
                session_id,
                tool_call_id=call_id,
                name=name,
                content="工具执行已由用户按 Ctrl+C 终止",
                status="cancelled",
                arguments={},
            )

        return self._record_terminal_marker(
            session_id,
            "本次回答已由用户按 Ctrl+C 终止。",
            status="cancelled",
        )

    def record_network_failure(self, session_id: str) -> bool:
        """网络重试耗尽时闭合当前问答，允许用户在同一 Session 重新发送。"""
        return self._record_terminal_marker(
            session_id,
            "本次回答因网络连接中断未完成，请重新发送问题。",
            status="network_error",
        )

    def record_cancelled_partial(self, session_id: str, content: str) -> bool:
        """保存 Ctrl+C 前已流式生成的文本，不接纳未完成的工具调用字段。"""
        if not content:
            return False
        cache = self._ensure_cache(session_id)
        if not cache or cache[-1].get("role") not in {"user", "tool"}:
            return False
        self.sessions.append(session_id, "assistant", content, {"status": "cancelled"})
        cache.append({"role": "assistant", "content": content})
        return True

    def restore_messages(self, session_id: str) -> list[dict[str, Any]]:
        """恢复索引指向的最新会话分段。"""
        return [dict(message) for message in self._ensure_cache(session_id)]

    def refresh_messages(self, session_id: str) -> list[dict[str, Any]]:
        """显式从最新 JSONL 重建内存消息缓存。"""
        self._message_cache[session_id] = self.sessions.restore(session_id)
        return self.restore_messages(session_id)

    def prepare_historical_tool_outputs(
        self,
        session_id: str,
        *,
        max_chars: int,
        head_ratio: float,
        tail_ratio: float,
    ) -> bool:
        """仅在下一用户任务开始前，对内存中的旧工具结果建立裁剪投影。"""
        if max_chars <= 0:
            return False
        messages = self._ensure_cache(session_id)
        changed = False
        for message in messages:
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            content = str(message["content"])
            if len(content) <= max_chars or "[历史工具输出已裁剪" in content:
                continue
            head = int(max_chars * head_ratio)
            tail = int(max_chars * tail_ratio)
            omitted = max(0, len(content) - head - tail)
            message["content"] = (
                f"{content[:head]}\n\n[历史工具输出已裁剪：原始 {len(content)} 字符，"
                f"省略 {omitted} 字符]\n\n{content[-tail:] if tail else ''}"
            )
            changed = True
        return changed

    def has_session(self, session_id: str) -> bool:
        """判断会话哈希是否可恢复。"""
        return self.sessions.exists(session_id)

    def list_sessions(self) -> list[dict[str, object]]:
        """返回供 CLI 展示的会话摘要。"""
        return self.sessions.list_sessions()

    def session_records(self, session_id: str) -> list[dict[str, object]]:
        """读取带时间戳的原始会话记录。"""
        return self.sessions.read_records(session_id)

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
            for record in self.session_records(session_id)
        )

    def active_filename(self, session_id: str) -> str:
        """返回会话当前 JSONL 文件名。"""
        return self.sessions.active_filename(session_id)

    def active_path(self, session_id: str) -> Path:
        return self.sessions.active_path(session_id)

    def session_created_at(self, session_id: str) -> str:
        return self.sessions.created_at(session_id)

    def latest_summary(self, session_id: str) -> str:
        return self.sessions.latest_summary(session_id)

    def invalidate_session_cache(self, session_id: str) -> None:
        self._message_cache.pop(session_id, None)

    def rollover_with_summary(
        self,
        session_id: str,
        summary: str,
        source_file: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> Path:
        """创建以 summary 记录开头的新会话分段。"""
        record: dict[str, object] = {
            "role": "summary",
            "content": summary,
            "source_file": source_file,
        }
        if metadata:
            record.update(metadata)
        result = self.sessions.rollover(session_id, [record])
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
    ) -> tuple[Path | None, Path]:
        """协调 Profile 与新分段写入；切段失败时恢复旧 Profile 状态。"""
        if not self.session_profiles_enabled:
            return None, self.rollover_with_summary(
                session_id,
                context_summary,
                source_file,
                metadata=summary_metadata,
            )
        profile_path = self.profiles.directory / f"{session_id}.md"
        profile_backup = profile_path.read_bytes() if profile_path.exists() else None
        index_backup = self.profiles.index_path.read_bytes() if self.profiles.index_path.exists() else None
        try:
            committed_profile = self.profiles.commit_session_profile(
                session_id,
                profile_markdown,
                source_file=source_file,
                conversation_turns=conversation_turns,
                records_processed=records_processed,
                tool_calls_processed=tool_calls_processed,
            )
            segment = self.rollover_with_summary(
                session_id,
                context_summary,
                source_file,
                metadata=summary_metadata,
            )
            return committed_profile, segment
        except Exception:
            if profile_backup is None:
                profile_path.unlink(missing_ok=True)
            else:
                profile_path.write_bytes(profile_backup)
            if index_backup is not None:
                self.profiles.index_path.write_bytes(index_backup)
            raise

    def _ensure_cache(self, session_id: str) -> list[dict[str, Any]]:
        if session_id not in self._message_cache:
            self._message_cache[session_id] = self.sessions.restore(session_id)
        return self._message_cache[session_id]

    def _record_terminal_marker(self, session_id: str, content: str, *, status: str) -> bool:
        cache = self._ensure_cache(session_id)
        if not cache or cache[-1].get("role") not in {"user", "tool"}:
            return False
        self.sessions.append(session_id, "assistant", content, {"status": status})
        cache.append({"role": "assistant", "content": content})
        return True


def _infer_agent_root(memory_root: Path) -> Path:
    if memory_root.name == "memory" and memory_root.parent.name == ".yy":
        return memory_root.parents[1]
    return memory_root


def _workspace_key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
