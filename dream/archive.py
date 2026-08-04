"""跨 workspace 扫描原始 Session JSONL，并建立用户证据。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from memory.models import SessionIndex, SessionRecord
from pydantic import ValidationError
from tzlocal import get_localzone_name

from .models import DreamDayArchive, DreamEvidence, DreamTranscriptRecord


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


class SessionArchiveReader:
    """只读扫描 `.yy/memory/session` 的全部索引与历史分段。"""

    def __init__(self, session_root: Path) -> None:
        self.session_root = session_root.resolve()

    def iter_day(
        self,
        selected_date: date,
        timezone_name: str,
        *,
        excluded_session_ids: set[str] | None = None,
    ) -> DreamDayArchive:
        zone_name = get_localzone_name() if timezone_name == "local" else timezone_name
        zone = ZoneInfo(zone_name)
        excluded = excluded_session_ids or set()
        records: list[DreamTranscriptRecord] = []
        evidence: list[DreamEvidence] = []
        sessions: set[str] = set()
        files: set[str] = set()
        if not self.session_root.exists():
            return DreamDayArchive(date=selected_date.isoformat(), timezone=zone_name)

        for index_path in sorted(self.session_root.rglob("index.json")):
            try:
                index = SessionIndex.model_validate_json(
                    index_path.read_text(encoding="utf-8"), strict=True,
                )
            except (OSError, ValidationError, ValueError):
                continue
            workspace_key = (
                "legacy" if index_path.parent == self.session_root else index_path.parent.name
            )
            for session_id, entry in index.sessions.items():
                if session_id in excluded:
                    continue
                for filename in entry.files:
                    path = index_path.parent / filename
                    if not path.is_file():
                        continue
                    for line_number, raw in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), 1,
                    ):
                        if not raw.strip():
                            continue
                        try:
                            record = SessionRecord.model_validate_json(raw)
                        except ValidationError:
                            continue
                        if getattr(record, "origin", None) in {"cron", "maintenance"}:
                            continue
                        try:
                            timestamp = _timestamp_in_zone(record.timestamp, zone)
                        except ValueError:
                            continue
                        if timestamp.date() != selected_date:
                            continue
                        if record.role not in {"user", "assistant"}:
                            continue
                        if not isinstance(record.content, str) or not record.content.strip():
                            continue
                        # 带 tool_calls 的 assistant 是编排消息，不作为自然语言语境。
                        if record.role == "assistant" and record.tool_calls:
                            continue
                        content = redact_secrets(record.content.strip())
                        if not content:
                            continue
                        evidence_id = None
                        if record.role == "user":
                            evidence_id = _evidence_id(
                                workspace_key, session_id, filename, line_number,
                                record.timestamp, content,
                            )
                            evidence.append(DreamEvidence(
                                evidence_id=evidence_id,
                                workspace_key=workspace_key,
                                session_id=session_id,
                                source_file=filename,
                                line_number=line_number,
                                timestamp=record.timestamp,
                                content=content,
                            ))
                        records.append(DreamTranscriptRecord(
                            role=record.role,
                            content=content,
                            timestamp=record.timestamp,
                            workspace_key=workspace_key,
                            session_id=session_id,
                            source_file=filename,
                            line_number=line_number,
                            evidence_id=evidence_id,
                        ))
                        sessions.add(session_id)
                        files.add(str(path))
        records.sort(key=lambda item: (item.timestamp, item.workspace_key, item.session_id, item.source_file, item.line_number))
        evidence.sort(key=lambda item: (item.timestamp, item.workspace_key, item.session_id, item.source_file, item.line_number))
        return DreamDayArchive(
            date=selected_date.isoformat(),
            timezone=zone_name,
            records=tuple(records),
            evidence=tuple(evidence),
            session_count=len(sessions),
            source_file_count=len(files),
        )


def redact_secrets(content: str) -> str:
    selected = content
    for pattern in _SECRET_PATTERNS:
        selected = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            selected,
        )
    return selected


def contains_secret(content: str) -> bool:
    return redact_secrets(content) != content


def _timestamp_in_zone(value: str, zone: ZoneInfo) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Session 时间戳无效：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _evidence_id(
    workspace_key: str,
    session_id: str,
    filename: str,
    line_number: int,
    timestamp: str,
    content: str,
) -> str:
    canonical = json.dumps(
        [workspace_key, session_id, filename, line_number, timestamp, content],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
