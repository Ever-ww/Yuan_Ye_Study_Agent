"""Append-only, hidden execution evidence for the stateless Dream runtime."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class DreamExecutionJournal:
    """Persist reproducibility evidence without creating a chat/Memory session.

    The journal intentionally stores hashes, source record locators and parsed
    structured outputs.  It never duplicates the full user transcript or the
    full provider prompt, both of which remain in their canonical stores.
    """

    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.execution_session_id = f"dream-exec-{run_id}"
        self.path = root / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, str] = {}
        if self.path.exists():
            for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                record_id = str(value["record_id"])
                content_hash = str(value["content_hash"])
                previous = self._records.setdefault(record_id, content_hash)
                if previous != content_hash:
                    raise RuntimeError(
                        f"Dream execution journal conflict at line {number}: {record_id}"
                    )

    def append_once(self, key: str, kind: str, payload: dict[str, Any]) -> str:
        canonical_payload = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        content_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        record_id = hashlib.sha256(
            f"{self.run_id}\0{key}".encode("utf-8"),
        ).hexdigest()
        existing = self._records.get(record_id)
        if existing is not None:
            if existing != content_hash:
                raise RuntimeError(f"Dream execution evidence conflict: {record_id}")
            return record_id
        record = {
            "record_id": record_id,
            "execution_session_id": self.execution_session_id,
            "run_id": self.run_id,
            "kind": kind,
            "payload": payload,
            "content_hash": content_hash,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        encoded = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._records[record_id] = content_hash
        return record_id


__all__ = ["DreamExecutionJournal"]
