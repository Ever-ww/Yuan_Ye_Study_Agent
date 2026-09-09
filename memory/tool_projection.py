"""Deterministic, provider-only previews of verifiable historical observations."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


MARKER = "[历史工具结果预览 v1]"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def observation_key(message: dict[str, Any]) -> tuple[str, str]:
    return str(message.get("tool_call_id") or ""), content_hash(str(message.get("content") or ""))


@dataclass(frozen=True)
class ToolOutputProjectionPolicy:
    # max_chars is the eligibility threshold, not the length of the preview.
    max_chars: int = 10000
    head_chars: int = 25
    tail_chars: int = 25
    protect_recent_groups: int = 1
    diagnostic_chars: int = 600

    @classmethod
    def from_config(cls, config: Any) -> "ToolOutputProjectionPolicy":
        return cls(
            max_chars=config.tool_output_max_chars,
            head_chars=config.tool_output_preview_head_chars,
            tail_chars=config.tool_output_preview_tail_chars,
            protect_recent_groups=config.tool_output_protect_recent_groups,
            diagnostic_chars=config.tool_output_diagnostic_max_chars,
        )


class ToolOutputProjector:
    def __init__(
        self, policy: ToolOutputProjectionPolicy, records: list[dict[str, Any]], *, current_run_id: str | None = None,
    ) -> None:
        self.policy = policy
        self.evidence: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in records:
            if current_run_id and record.get("run_id") == current_run_id:
                continue
            if record.get("role") == "tool" and isinstance(record.get("content"), str):
                self.evidence.setdefault(observation_key(record), []).append(record)
        historical_groups = [group for group in self._complete_groups(records)
                             if not current_run_id or all(records[i].get("run_id") != current_run_id for i in group)]
        self.protected_keys = {
            observation_key(records[index])
            for group in historical_groups[-max(1, policy.protect_recent_groups):]
            for index in group
        }

    @staticmethod
    def _complete_groups(messages: list[dict[str, Any]]) -> list[list[int]]:
        groups: list[list[int]] = []
        for index, message in enumerate(messages):
            calls = message.get("tool_calls")
            if message.get("role") != "assistant" or not isinstance(calls, list) or not calls:
                continue
            ids = [call.get("id") for call in calls if isinstance(call, dict)]
            if len(ids) != len(calls) or any(not isinstance(value, str) or not value for value in ids):
                continue
            if len(set(ids)) != len(ids):
                continue
            results: list[int] = []
            for following in range(index + 1, len(messages)):
                if messages[following].get("role") != "tool":
                    break
                results.append(following)
            received = [messages[value].get("tool_call_id") for value in results]
            if len(received) == len(ids) and set(received) == set(ids):
                groups.append(results)
        return groups

    def project(self, messages: list[dict[str, Any]], *, protect_current_turn: bool = False) -> int:
        """Modify only the supplied projection; incomplete/orphan results stay intact."""
        if self.policy.max_chars <= 0:
            return 0
        groups = self._complete_groups(messages)
        protected = max(1, self.policy.protect_recent_groups)
        eligible = {index for group in groups[:-protected] for index in group}
        if protect_current_turn:
            last_user = max((i for i, value in enumerate(messages) if value.get("role") == "user"), default=-1)
            eligible = {i for i in eligible if i < last_user}
        saved = 0
        for index in sorted(eligible):
            message = messages[index]
            body = message.get("content")
            if not isinstance(body, str) or len(body) <= self.policy.max_chars:
                continue
            if observation_key(message) in self.protected_keys:
                continue
            candidates = self.evidence.get(observation_key(message), [])
            # Unproven or ambiguous legacy identities must never be guessed.
            if len(candidates) != 1:
                continue
            record = candidates[0]
            if record.get("name") != message.get("name"):
                continue
            preview = self.render(record)
            if len(preview) >= len(body):
                continue
            message["content"] = preview
            saved += len(body) - len(preview)
        return saved

    def render(self, record: dict[str, Any]) -> str:
        body = record["content"]
        selector = {"expected_content_hash": content_hash(body)}
        if record.get("record_id"):
            selector["record_id"] = record["record_id"]
        else:
            selector["tool_call_id"] = record["tool_call_id"]
            if record.get("run_id"):
                selector["run_id"] = record["run_id"]
        metadata = {
            "name": record.get("name"), "status": record.get("status", "unknown"),
            "tool_call_id": record.get("tool_call_id"), "record_id": record.get("record_id"),
            "run_id": record.get("run_id"), "original_chars": len(body),
            "session_history": selector,
        }
        head = body[:self.policy.head_chars]
        tail = body[-self.policy.tail_chars:] if self.policy.tail_chars else ""
        diagnostic = _diagnostic(record, self.policy.diagnostic_chars)
        return (
            MARKER + "\n" + json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n历史证据，不代表当前文件/服务状态；需要完整内容时按上述参数回查，不必重跑工具。"
            + ("\n结构/诊断摘要（确定性摘录，可能不完整）：" + diagnostic if diagnostic else "")
            + "\n正文首尾：\n" + head + "\n…[中间已省略]…\n" + tail
        )


def _diagnostic(record: dict[str, Any], budget: int) -> str:
    """Bounded, non-LLM extractors. Never copy tool arguments or hidden audit fields."""
    if budget <= 0:
        return ""
    body = record["content"]
    details: list[str] = []
    parsed: Any = None
    # Avoid parsing unbounded serialized artifacts just to build a preview.
    if len(body) <= 1_000_000 and body.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(body)
        except (ValueError, RecursionError):
            pass
    if isinstance(parsed, dict):
        keys = ("error", "message", "returncode", "exit_code", "status", "failed",
                "path", "format", "selected_start", "selected_end", "offset_chars",
                "truncated", "next_offset_chars", "total", "count")
        for key in keys:
            if key in parsed:
                value = parsed[key]
                if isinstance(value, (str, int, float, bool)) or value is None:
                    details.append(f"{key}={str(value)[:200]}")
        details.append("keys=" + ",".join(str(key)[:40] for key in list(parsed)[:12]))
    elif isinstance(parsed, list):
        details.append(f"JSON array count={len(parsed)}")
    # Keep failure/test evidence even when a shell tool reports 'success' but its
    # command printed failures. Each excerpt is bounded independently.
    failure = re.compile(r"error|exception|traceback|\bfailed\b|\bfailures?\b|\bassert\w*|exit.code|returncode|denied|timeout|permission|错误|失败|超时", re.I)
    excerpts = []
    for match in re.finditer(r"[^\r\n]+", body):
        line = match.group()
        hit = failure.search(line)
        if hit:
            start = max(0, hit.start() - 60)
            excerpts.append(line[start:start + 200])
            if len(excerpts) >= 4:
                break
    if excerpts:
        details = excerpts + details
    elif record.get("status") in {"error", "failed", "cancelled", "skipped"}:
        details.insert(0, body[:min(200, budget)])
    if record.get("name") in {"search_workspace", "web_search"}:
        details.extend(line[:160] for line in body.splitlines()[:3])
    return " | ".join(details)[:budget]
