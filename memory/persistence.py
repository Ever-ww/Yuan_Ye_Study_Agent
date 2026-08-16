"""Conversation persistence projections and provider-only context guards."""

from __future__ import annotations

import hashlib
import copy
import re
from typing import Any


EPHEMERAL_CONTEXT_OPEN = '<harness_runtime_context ephemeral="true">'
EPHEMERAL_CONTEXT_CLOSE = "</harness_runtime_context>"
AGENT_EPHEMERAL_CONTEXT_OPEN = '<agent_runtime_context ephemeral="true">'
AGENT_EPHEMERAL_CONTEXT_CLOSE = "</agent_runtime_context>"

_EPHEMERAL_MARKERS = (
    EPHEMERAL_CONTEXT_OPEN,
    EPHEMERAL_CONTEXT_CLOSE,
    AGENT_EPHEMERAL_CONTEXT_OPEN,
    AGENT_EPHEMERAL_CONTEXT_CLOSE,
)


class SessionPersistenceProjection:
    """Reject provider-only Harness context before conversation persistence."""

    @staticmethod
    def assert_persistable(content: str) -> str:
        if any(marker in content for marker in _EPHEMERAL_MARKERS):
            raise ValueError("Ephemeral runtime context must never be persisted in Session data")
        return content

    @staticmethod
    def assert_no_ephemeral(value: Any) -> None:
        if isinstance(value, str):
            SessionPersistenceProjection.assert_persistable(value)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                SessionPersistenceProjection.assert_no_ephemeral(key)
                SessionPersistenceProjection.assert_no_ephemeral(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                SessionPersistenceProjection.assert_no_ephemeral(item)

    @staticmethod
    def strip_ephemeral(content: str) -> str:
        """Remove a complete echoed envelope; reject incomplete marker fragments."""
        projected = content
        for name in ("harness_runtime_context", "agent_runtime_context"):
            pattern = re.compile(
                rf"\n*<{name} ephemeral=\"true\">.*?</{name}>\s*",
                re.DOTALL,
            )
            projected = pattern.sub("", projected).strip()
        projected = re.sub(
            r"<user_query>\s*(.*?)\s*</user_query>",
            lambda match: match.group(1),
            projected,
            flags=re.DOTALL,
        ).strip()
        return SessionPersistenceProjection.assert_persistable(projected)

    @staticmethod
    def from_runtime_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project provider messages into a form safe for failure snapshots and trace export."""
        projected = copy.deepcopy(messages)
        for message in projected:
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = SessionPersistenceProjection.strip_ephemeral(content)
        return projected

    @staticmethod
    def envelope_fingerprint(payload: str, *, schema_version: int, source_revision: int | None) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "source_revision": source_revision,
            "content_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }
