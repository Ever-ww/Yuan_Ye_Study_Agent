"""Immutable provider projections, separate from canonical conversation content.

Hashes detect accidental corruption, not malicious edits to the Agent Home.
Only request reconstruction reads these fields; recall and compression read content.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProviderContextRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    context_epoch: str
    query_hash: str
    fragments: dict[str, str]
    fragment_hashes: dict[str, str]
    content_hash: str
    origin_refs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def verify(self) -> "ProviderContextRecord":
        if self.fragment_hashes != {key: digest(value) for key, value in self.fragments.items()}:
            raise ValueError("Provider context fragment hash mismatch")
        if self.content_hash != self.compute_hash(self.fragments):
            raise ValueError("Provider context content hash mismatch")
        return self

    @staticmethod
    def compute_hash(fragments: dict[str, str]) -> str:
        return digest(json.dumps(fragments, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    @classmethod
    def create(cls, query: str, epoch: str, fragments: dict[str, str], origin_refs=None):
        return cls(
            context_epoch=epoch, query_hash=digest(query), fragments=fragments,
            fragment_hashes={key: digest(value) for key, value in fragments.items()},
            content_hash=cls.compute_hash(fragments), origin_refs=origin_refs or {},
        )

    def render(self, query: str) -> str:
        if digest(query) != self.query_hash:
            raise ValueError("Provider context belongs to a different user query")
        if not self.fragments:
            return query
        priority = {"agent": -80, "memory": -70, "continuity": -60, "harness": -50}
        names = sorted(self.fragments, key=lambda name: (priority.get(name, 0), name))
        return f"<user_query>\n{query}\n</user_query>\n\n" + "\n\n".join(self.fragments[key] for key in names)


def effective_contexts(records: list[dict[str, Any]]) -> dict[str, ProviderContextRecord]:
    """Resolve epoch-local, append-only amendments created by mid-Turn compaction."""
    users = {str(r.get("record_id")): r for r in records if r.get("role") == "user"}
    result = {}
    for record in records:
        raw = record.get("provider_context")
        if raw is None:
            continue
        packet = ProviderContextRecord.model_validate(raw)
        target = str(record.get("context_target_record_id") or record.get("record_id"))
        user = users.get(target)
        if user is None:
            raise ValueError("Provider context has no visible user record")
        packet.render(str(user["content"]))  # verify query binding, even on clean reads
        result[target] = packet
    return result


def context_baseline(
    records: list[dict[str, Any]],
    *,
    before_record_id: str | None = None,
) -> dict[str, str]:
    """Resolve the effective fragment baseline before a selected user record.

    Provider-context amendments are append-only and may physically occur after
    their target user record.  Resolve all amendments first, then stop at the
    logical user boundary instead of slicing the raw record list.
    """
    contexts = effective_contexts(records)
    baseline: dict[str, str] = {}
    for record in records:
        if record.get("role") == "user":
            record_id = str(record.get("record_id"))
            if before_record_id is not None and record_id == before_record_id:
                break
            packet = contexts.get(record_id)
            if packet:
                baseline.update(packet.fragments)
    return baseline


def prepare_context(query: str, epoch: str, current: dict[str, str],
                    baseline: dict[str, str], origin_refs=None) -> ProviderContextRecord:
    from gateway.audit import AuditSanitizer
    from .persistence import SessionPersistenceProjection

    SessionPersistenceProjection.assert_persistable(query)
    current = AuditSanitizer.sanitize(current)
    tags = {"memory": "relevant_memory", "continuity": "continuity_fragment",
            "harness": "harness_runtime_context"}
    for key in baseline.keys() - current.keys():
        if key in tags:
            tag = tags[key]
            current[key] = f'<{tag} ephemeral="true">\n{{"active":false}}\n</{tag}>'
    return ProviderContextRecord.create(
        query, epoch, {key: value for key, value in current.items() if baseline.get(key) != value}, origin_refs,
    )
