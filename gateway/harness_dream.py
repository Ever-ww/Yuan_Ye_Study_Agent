"""Daily Harness change discovery and durable DREAM entry orchestration."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from tzlocal import get_localzone_name

from backup import SensitiveEnvSanitizer


HarnessDreamOutcome = Literal[
    "no_changes", "success", "deferred", "blocked", "failed", "unknown",
    "frozen", "restart_wait_timeout",
]


class HarnessDreamMergeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    merge_event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: str
    invocation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    trigger: Literal["manual", "error", "capability"]
    source_identity: str = Field(pattern=r"^[0-9a-f]{16}$")
    base_commit: str = Field(min_length=7)
    verified_commit: str = Field(min_length=7)
    merged_commit: str = Field(min_length=7)
    target_branch: str = Field(min_length=1)
    changed_files: tuple[str, ...] = ()


class HarnessDreamChangeSet(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    timezone: str = Field(min_length=1)
    cutoff_at: str = Field(min_length=1)
    source_identity: str = Field(pattern=r"^[0-9a-f]{16}$")
    merge_event_ids: tuple[str, ...] = ()
    invocation_ids: tuple[str, ...] = ()
    merged_commits: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    changeset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stable_key: str = Field(min_length=1)
    last_event: dict[str, str] = Field(default_factory=dict)
    evidence: tuple[HarnessDreamMergeEvidence, ...] = ()


class DreamEvolutionContext(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    changeset: HarnessDreamChangeSet
    generation: int = Field(ge=1)
    automatic: bool


class HarnessDreamRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: HarnessDreamOutcome
    date: str
    stable_key: str = ""
    generation: int = Field(default=0, ge=0)
    run_id: str = ""
    message: str
    invocation_id: str = ""
    merged_commit: str = ""
    changed_files: tuple[str, ...] = ()
    restart_required: bool = False


class HarnessDreamStatus(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    enabled: bool
    frozen: bool
    freeze: dict[str, Any] | None = None
    latest: dict[str, Any] | None = None


class HarnessRevertProposal(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    proposal_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    stable_key: str
    operation_run_id: str
    source_identity: str
    merged_commit: str
    base_head: str
    candidate_commit: str = ""
    candidate_branch: str
    worktree_path: str
    validation_summary: tuple[str, ...] = ()
    status: Literal["proposed", "blocked", "approved", "rejected", "merged"]
    created_at: str


class HarnessDreamChangeScanner:
    """Build one immutable daily changeset from durable Harness merge receipts."""

    _ELIGIBLE = frozenset({"manual", "error", "capability"})

    def __init__(self, agent_root: Path, source_root: Path, timezone_name: str) -> None:
        self.agent_root = agent_root.resolve()
        self.source_root = source_root.resolve()
        self.source_identity = hashlib.sha256(
            str(self.source_root).casefold().encode("utf-8"),
        ).hexdigest()[:16]
        self.timezone_name = get_localzone_name() if timezone_name == "local" else timezone_name
        self.zone = ZoneInfo(self.timezone_name)
        self.audit_root = (
            self.agent_root / ".yy" / "harness-evolution" / "invocations" / self.source_identity
        )

    def scan(self, selected_date: date, *, cutoff_at: datetime) -> HarnessDreamChangeSet:
        cutoff = self._in_zone(cutoff_at)
        evidence: list[HarnessDreamMergeEvidence] = []
        if self.audit_root.is_dir():
            for path in sorted(self.audit_root.glob("*.jsonl")):
                evidence.extend(self._read_invocation(path, selected_date, cutoff))
        evidence.sort(key=lambda item: (item.occurred_at, item.merge_event_id))
        merge_event_ids = tuple(item.merge_event_id for item in evidence)
        invocation_ids = tuple(dict.fromkeys(item.invocation_id for item in evidence))
        commits = tuple(dict.fromkeys(item.merged_commit for item in evidence))
        changed_files = tuple(sorted({path for item in evidence for path in item.changed_files}))
        canonical = json.dumps(
            {
                "date": selected_date.isoformat(),
                "timezone": self.timezone_name,
                "cutoff_at": cutoff.isoformat(timespec="seconds"),
                "source_identity": self.source_identity,
                "merge_event_ids": merge_event_ids,
                "merged_commits": commits,
                "changed_files": changed_files,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        suffix = digest if evidence else f"no-changes:{digest}"
        last = evidence[-1] if evidence else None
        return HarnessDreamChangeSet(
            date=selected_date.isoformat(), timezone=self.timezone_name,
            cutoff_at=cutoff.isoformat(timespec="seconds"),
            source_identity=self.source_identity,
            merge_event_ids=merge_event_ids, invocation_ids=invocation_ids,
            merged_commits=commits, changed_files=changed_files,
            changeset_hash=digest,
            stable_key=f"harness-dream:{self.source_identity}:{selected_date.isoformat()}:{suffix}",
            last_event=(
                {"occurred_at": last.occurred_at, "merge_event_id": last.merge_event_id}
                if last else {}
            ),
            evidence=tuple(evidence),
        )

    def _read_invocation(
        self, path: Path, selected_date: date, cutoff: datetime,
    ) -> list[HarnessDreamMergeEvidence]:
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[tuple[int, dict[str, Any]]] = []
        for line_no, raw in enumerate(raw_lines, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return []
            if not isinstance(value, dict):
                return []
            records.append((line_no, value))
        started = next((value for _, value in records if value.get("event") == "invocation_started"), {})
        trigger = str(started.get("trigger") or "")
        invocation_id = str(started.get("invocation_id") or path.stem)
        if trigger not in self._ELIGIBLE or not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
            return []
        intent: dict[str, Any] = {}
        selected: list[HarnessDreamMergeEvidence] = []
        for line_no, record in records:
            if record.get("event") == "merge_intent":
                intent = record
                continue
            if record.get("event") != "merge_committed":
                continue
            occurred = self._parse_timestamp(record.get("occurred_at") or record.get("timestamp"))
            if occurred is None or occurred > cutoff or occurred.date() != selected_date:
                continue
            verified = str(record.get("verified_commit") or intent.get("verified_commit") or "")
            merged = str(record.get("merged_commit") or "")
            base = str(record.get("base_commit") or intent.get("base_commit") or "")
            branch = str(record.get("target_branch") or intent.get("target_branch") or "")
            if not verified or merged != verified or not base or not branch:
                continue
            if not self._git_proves_merge(branch, base, verified):
                continue
            files = record.get("changed_files") or intent.get("changed_files")
            if not isinstance(files, list):
                files = self._changed_files(base, verified)
            clean_files = tuple(sorted({str(item).replace("\\", "/") for item in files if str(item).strip()}))
            event_id = str(record.get("merge_event_id") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", event_id):
                event_id = hashlib.sha256(
                    f"{self.source_identity}:{invocation_id}:{line_no}:{merged}:{occurred.isoformat()}".encode("utf-8"),
                ).hexdigest()
            selected.append(HarnessDreamMergeEvidence(
                merge_event_id=event_id,
                occurred_at=occurred.isoformat(timespec="seconds"),
                invocation_id=invocation_id, trigger=trigger,
                source_identity=self.source_identity,
                base_commit=base, verified_commit=verified, merged_commit=merged,
                target_branch=branch, changed_files=clean_files,
            ))
        return selected

    def _git_proves_merge(self, branch: str, base: str, verified: str) -> bool:
        branch_head = self._git("rev-parse", "--verify", branch)
        if not branch_head:
            return False
        return (
            self._git_status("merge-base", "--is-ancestor", base, verified) == 0
            and self._git_status("merge-base", "--is-ancestor", verified, branch_head) == 0
        )

    def _changed_files(self, base: str, verified: str) -> list[str]:
        value = self._git("diff", "--name-only", f"{base}..{verified}")
        return value.splitlines() if value else []

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=self.source_root, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=SensitiveEnvSanitizer.subprocess_env(),
        )
        return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else ""

    def _git_status(self, *arguments: str) -> int:
        return subprocess.run(
            ["git", *arguments], cwd=self.source_root, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=SensitiveEnvSanitizer.subprocess_env(),
        ).returncode

    def _parse_timestamp(self, raw: Any) -> datetime | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.zone)
        return parsed.astimezone(self.zone)

    def _in_zone(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.zone)
        return value.astimezone(self.zone)


__all__ = [
    "DreamEvolutionContext", "HarnessDreamChangeScanner", "HarnessDreamChangeSet",
    "HarnessDreamMergeEvidence", "HarnessDreamRunResult", "HarnessDreamStatus",
    "HarnessRevertProposal",
]
