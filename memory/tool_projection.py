"""Deterministic provider-only trimming of historical Tool observations.

This module never mutates canonical Session records.  It is invoked by an
independent MODEL_BEFORE Hook, not by context compression.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from memory.turn_boundary import protected_turn_start


MARKER = "[如想查看全部tool call输出内容，请调用工具session_read："
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ENGLISH_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def observation_key(message: dict[str, Any]) -> tuple[str, str]:
    return str(message.get("tool_call_id") or ""), content_hash(str(message.get("content") or ""))


@dataclass(frozen=True)
class ToolOutputProjectionPolicy:
    cjk_threshold_chars: int = 1000
    english_threshold_words: int = 1000
    # A hard character fallback also bounds code, base64, and punctuation-heavy
    # observations that contain neither 1000 Han characters nor 1000 words.
    max_chars: int = 10000
    head_chars: int = 427
    tail_chars: int = 427
    head_words: int = 427
    tail_words: int = 427

    @classmethod
    def from_config(cls, config: Any) -> "ToolOutputProjectionPolicy":
        return cls(
            cjk_threshold_chars=config.tool_output_cjk_threshold_chars,
            english_threshold_words=config.tool_output_english_threshold_words,
            max_chars=config.tool_output_max_chars,
            head_chars=config.tool_output_preview_head_chars,
            tail_chars=config.tool_output_preview_tail_chars,
            head_words=config.tool_output_preview_head_words,
            tail_words=config.tool_output_preview_tail_words,
        )

    def should_trim(self, content: str) -> bool:
        return bool(
            _exceeds_matches(_CJK, content, self.cjk_threshold_chars)
            or (
                _exceeds_matches(_ENGLISH_WORD, content, self.english_threshold_words)
            )
            or (self.max_chars > 0 and len(content) > self.max_chars)
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
        self.current_run_id = current_run_id

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

    def project(
        self,
        messages: list[dict[str, Any]],
        *,
        protect_current_turn: bool = True,
    ) -> int:
        """Trim old complete Tool groups while preserving the latest conversation.

        The current and previous user blocks remain byte-for-byte intact.
        Incomplete/orphan results and records without an exact durable selector
        also remain intact.
        """
        if not any((self.policy.cjk_threshold_chars, self.policy.english_threshold_words,
                    self.policy.max_chars)):
            return 0
        groups = self._complete_groups(messages)
        boundary = protected_turn_start(messages)
        # Keep the argument for compatibility; protection cannot be disabled.
        del protect_current_turn
        eligible = {index for group in groups for index in group if index < boundary}
        saved = 0
        for index in sorted(eligible):
            message = messages[index]
            body = message.get("content")
            if not isinstance(body, str) or not self.policy.should_trim(body):
                continue
            candidates = self.evidence.get(observation_key(message), [])
            # Unproven or ambiguous legacy identities must never be guessed.
            if len(candidates) != 1:
                continue
            record = candidates[0]
            if record.get("name") != message.get("name"):
                continue
            if not isinstance(record.get("record_id"), str) or not isinstance(
                record.get("session_file"), str,
            ):
                # A preview without an exact, runtime-bound recovery selector
                # would discard information the model cannot retrieve again.
                continue
            preview = self.render(record)
            if len(preview) >= len(body):
                continue
            message["content"] = preview
            saved += max(0, len(body) - len(preview))
        return saved

    def render(self, record: dict[str, Any]) -> str:
        body = record["content"]
        selector = {
            "session_id": record["session_file"],
            "record_id": record["record_id"],
            "offset": 0,
            "limited": 1,
        }
        # Mixed output crossing the CJK threshold uses character boundaries;
        # otherwise English threshold crossings preserve whole words.
        if (not _exceeds_matches(_CJK, body, self.policy.cjk_threshold_chars)
                and _exceeds_matches(_ENGLISH_WORD, body, self.policy.english_threshold_words)):
            words = list(_ENGLISH_WORD.finditer(body))
            if len(words) <= self.policy.head_words + self.policy.tail_words:
                return body
            head = body[:words[self.policy.head_words - 1].end()] if self.policy.head_words else ""
            tail = body[words[-self.policy.tail_words].start():] if self.policy.tail_words else ""
        else:
            if len(body) <= self.policy.head_chars + self.policy.tail_chars:
                return body
            head = body[:self.policy.head_chars]
            tail = body[-self.policy.tail_chars:] if self.policy.tail_chars else ""
        instruction = (
            MARKER
            + json.dumps(selector, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "]"
        )
        return head + "\n" + instruction + "\n" + tail


def _exceeds_matches(pattern: re.Pattern[str], content: str, limit: int) -> bool:
    """Stop counting as soon as the configured threshold is crossed."""
    if limit <= 0:
        return False
    for count, _ in enumerate(pattern.finditer(content), 1):
        if count > limit:
            return True
    return False
