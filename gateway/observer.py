"""Gateway Core lifecycle, persistence and isolation for Runtime Observer plugins."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
import json
import os
import queue
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from Agent.config import RuntimeConfig
from Agent.hook import HookEvent, HookPoint, HookRegistry
from Agent.models import build_provider
from Agent.observer import (
    IntentAlignmentStatus,
    ObserverEvidence,
    ObserverPlugin,
    ObserverState,
    ObserverToolLoop,
    VisibleObserverEvent,
    render_observer_progress,
)
from Agent.retry import ModelRetryPolicy
from Agent.resources import RuntimeContributionKind, RuntimeProfile
from tool import AsyncToolRegistry
from gateway.event_store import EventStore, canonical_json
from gateway.models import GatewayEventEnvelope, RunRecord, now_iso


_OBSERVER_OUTPUT_TYPES = {
    "observer_progress", "observer_correction_proposed",
    "observer_correction_decided", "observer_skill_candidate",
}
_TERMINAL_TYPES = {
    # Only user-facing terminal facts finalize the Observer. ``run_terminal``
    # is an internal snapshot transition emitted during FINALIZING and occurs
    # before ``run_completed`` / ``run_failed``. Treating it as terminal makes
    # a successful Run appear as the ambiguous warning "任务已结束".
    "run_completed", "run_failed", "run_cancelled", "run_interrupted",
}
_VISIBLE_TYPES = {
    "run_queued", "run_started", "text", "tool_requested", "tool_completed",
    "model_retry", "model_reconnected", "compression_started",
    "context_compressed", "compression_fallback", "approval_requested", "final",
    *_TERMINAL_TYPES,
}
# Updating Observer state is intentionally coarser than storing visible-event
# evidence. Streaming text remains durable input evidence, but running one
# Observer model call per token would serialize and throttle the Main Agent.
_OBSERVER_MODEL_TYPES = {
    "run_started", "tool_requested", "tool_completed", "model_retry",
    "model_reconnected", "compression_started", "context_compressed",
    "compression_fallback", "approval_requested", *_TERMINAL_TYPES,
}


@dataclass(frozen=True)
class ObserverOutputEvent:
    event_type: str
    payload: dict[str, Any]


class ObserverPluginTimeout(RuntimeError):
    """A capability-layer Observer exceeded its bounded execution time."""


class ObserverModelFailure(RuntimeError):
    """The isolated Observer model path failed; this is not a bad plugin version."""


class ObserverModelTimeout(ObserverModelFailure):
    """The Observer model did not produce state inside its configured deadline."""


class ObserverModelOutputError(ObserverModelFailure):
    """The model returned content that cannot become a valid ObserverState."""


class _ObserverCallbackTimeout(RuntimeError):
    pass


class _ObserverCallbackWorker:
    """Single daemon worker which can be abandoned after an in-process timeout."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[
            tuple[Callable[[], object], queue.Queue[tuple[bool, object]]] | None
        ] = queue.Queue()
        self._thread = threading.Thread(
            target=self._serve, name="yy-observer-plugin", daemon=True,
        )
        self._thread.start()

    def invoke(
        self, callback: Callable[[], object], *, timeout: float,
    ) -> object:
        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        self._jobs.put((callback, result_queue))
        try:
            succeeded, result = result_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise _ObserverCallbackTimeout() from exc
        if not succeeded:
            if isinstance(result, BaseException):
                raise result
            raise RuntimeError(f"Observer plugin raised {type(result).__name__}")
        return result

    def close(self) -> None:
        self._jobs.put(None)

    def _serve(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            callback, result_queue = job
            try:
                result_queue.put((True, callback()))
            except BaseException as exc:  # plugin BaseException must not kill Core
                result_queue.put((False, exc))


class _ObserverNoMemory:
    """Non-persistent Memory facade for the dedicated Observer AgentRuntime."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def has_session(self, session_id: str) -> bool:
        del session_id
        return True

    def session_created_at(self, session_id: str) -> str:
        del session_id
        return now_iso()

    def active_path(self, session_id: str) -> Path:
        return self.root / ".yy" / "observer" / f"ephemeral-{session_id}.jsonl"

    def prompt_context(self, session_id: str | None = None) -> str:
        del session_id
        return ""

    def latest_summary(self, session_id: str) -> str:
        del session_id
        return ""


ObserverProviderFactory = Callable[[], Any]


class ObserverAgentRuntime:
    """Run one recursive state update through the stable AgentRuntime core."""

    def __init__(
        self, config: RuntimeConfig, *, provider_factory: ObserverProviderFactory | None = None,
    ) -> None:
        self.config = config
        self.provider_factory = provider_factory

    async def complete(self, messages: tuple[dict[str, str], ...]) -> str:
        from Agent.runtime.engine import AgentRuntime

        hooks = HookRegistry()

        async def inject(event: HookEvent) -> None:
            event.data["messages"] = [dict(item) for item in messages]
            event.data["tools"] = []

        hooks.register(HookPoint.MODEL_BEFORE, inject, priority=-100)
        selected = self.config.model_copy(update={
            "model": self.config.observer_model or self.config.model,
            # Observer has no filesystem capabilities.  Do not inherit the
            # Gateway process cwd: it can legitimately be the user's home,
            # which the logical-path guard rejects as a task workspace.
            "workspace_root": self.config.agent_root / "observer-runtime",
            "stream": False,
            "compression_threshold_tokens": 0,
            "max_steps": 1,
            "max_parallel_tool_calls": 1,
        })
        provider = (
            self.provider_factory()
            if self.provider_factory is not None
            else build_provider(
                selected.provider,
                selected.model,
                base_url=selected.base_url,
                api_key=selected.api_key,
                stream=False,
                use_system_proxy=selected.use_system_proxy,
                proxy_url=selected.proxy_url,
            )
        )
        runtime = AgentRuntime(
            selected,
            provider=provider,
            tools=AsyncToolRegistry(),
            memory=_ObserverNoMemory(selected.agent_root),
            hooks=hooks,
            enable_context_processing=False,
            enable_skills=False,
            enable_subagent=False,
            enable_sandbox=False,
            enable_extensions=False,
            enable_cron=False,
            enable_references=False,
            enable_paper_library=False,
            retry_policy=ModelRetryPolicy(max_attempts=1, delay_seconds=0),
            raise_errors=True,
            runtime_profile="maintenance",
        )
        result = await runtime.run("Update Observer state from the visible event")
        if not result.completed:
            raise ObserverModelFailure("Observer AgentRuntime did not complete")
        return result.answer


class VisibleEventProjection:
    """Project only data already rendered by the normal CLI/Web experience."""

    @staticmethod
    def project(event: GatewayEventEnvelope) -> VisibleObserverEvent | None:
        if event.type in _OBSERVER_OUTPUT_TYPES or event.type not in _VISIBLE_TYPES:
            return None
        payload = event.payload
        content = ""
        tool_name: str | None = None
        tool_status: str | None = None
        if event.type == "text":
            content = str(payload.get("content") or "")
        elif event.type in {"final", "run_completed"}:
            content = str(payload.get("answer") or payload.get("message") or "")
        elif event.type in {"run_failed", "run_cancelled", "run_interrupted"}:
            content = str(payload.get("message") or "")
        elif event.type in {"tool_requested", "tool_completed"}:
            tool_name = str(payload.get("name") or "") or None
            tool_status = str(payload.get("status") or "") or None
            content = f"{event.type}:{tool_name or 'tool'}:{tool_status or ''}"
        elif event.type == "approval_requested":
            # Arguments can contain paths, user data and credentials.  The UI-visible
            # tool name is sufficient for an Observer progress projection.
            tool_name = str(payload.get("tool_name") or "") or None
            content = f"等待工具审批：{tool_name or 'tool'}"
        else:
            content = str(payload.get("message") or "")
        return VisibleObserverEvent(
            event_id=event.event_id,
            run_id=event.run_id,
            stream_sequence=event.stream_sequence or event.sequence,
            event_type=event.type,
            occurred_at=event.timestamp,
            content=_limit(content),
            tool_name=tool_name,
            tool_status=tool_status,
        )


class ObserverStateStore:
    """Core durable store; state and consumed offset commit in one transaction."""

    def __init__(self, database_path: Path, *, write_gate: Any = None) -> None:
        self.database_path = database_path.resolve()
        self.write_gate = write_gate

    def instance(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM observer_instances WHERE run_id=?", (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def commit_event(
        self,
        *,
        run: RunRecord,
        generation_id: str,
        plugin: ObserverPlugin,
        runtime_profile: RuntimeProfile,
        trigger: str,
        event: GatewayEventEnvelope,
        visible: VisibleObserverEvent | None,
        previous: ObserverState,
        updated: ObserverState,
    ) -> bool:
        sequence = int(event.stream_sequence or event.sequence)
        timestamp = now_iso()
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM observer_instances WHERE run_id=?", (run.run_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO observer_instances(run_id,session_id,generation_id,"
                    "observer_plugin_id,observer_plugin_version,state_schema_version,"
                    "runtime_role,agent_role,runtime_profile,trigger_name,state_json,"
                    "last_event_offset,status,revision,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,'active',0,?,?)",
                    (
                        run.run_id, run.session_id, generation_id, plugin.plugin_id,
                        plugin.plugin_version, plugin.state_schema_version,
                        _runtime_role(runtime_profile), _agent_role(runtime_profile),
                        runtime_profile.value, trigger, previous.model_dump_json(),
                        timestamp, timestamp,
                    ),
                )
                existing_offset, revision = 0, 0
            else:
                existing_offset, revision = int(existing["last_event_offset"]), int(existing["revision"])
                if (
                    str(existing["generation_id"]) != generation_id
                    or str(existing["observer_plugin_id"]) != plugin.plugin_id
                    or str(existing["observer_plugin_version"]) != plugin.plugin_version
                ):
                    raise RuntimeError("Observer plugin identity changed within one Run")
            if sequence <= existing_offset:
                connection.commit()
                return False
            duplicate = connection.execute(
                "SELECT 1 FROM observer_visible_events WHERE event_id=?", (event.event_id,),
            ).fetchone()
            if duplicate is not None:
                connection.commit()
                return False
            connection.execute(
                "INSERT INTO observer_visible_events(event_id,run_id,stream_sequence,"
                "visible_event_json,visible,created_at) VALUES(?,?,?,?,?,?)",
                (
                    event.event_id, run.run_id, sequence,
                    visible.model_dump_json() if visible is not None else None,
                    int(visible is not None), timestamp,
                ),
            )
            if visible is not None and event.type == "tool_requested":
                self._append_tool_loop_in_transaction(connection, run.run_id, event, timestamp)
            changed = connection.execute(
                "UPDATE observer_instances SET session_id=?,state_json=?,last_event_offset=?,"
                "revision=revision+1,updated_at=? WHERE run_id=? AND revision=? "
                "AND last_event_offset=?",
                (
                    run.session_id, updated.model_dump_json(), sequence, timestamp,
                    run.run_id, revision, existing_offset,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Observer state/offset CAS conflict")
            connection.commit()
            return True

    def mark_failed(self, run_id: str) -> None:
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE observer_instances SET status='failed',revision=revision+1,"
                "updated_at=? WHERE run_id=? AND status='active'", (now_iso(), run_id),
            )
            connection.commit()

    def finalize_or_propose(
        self, run_id: str, *, timeout_seconds: int,
    ) -> tuple[ObserverEvidence | None, dict[str, Any] | None]:
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM observer_instances WHERE run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None, None
            evidence = connection.execute(
                "SELECT evidence_json FROM observer_evidence WHERE run_id=?", (run_id,),
            ).fetchone()
            if evidence is not None:
                connection.commit()
                return ObserverEvidence.model_validate_json(evidence["evidence_json"], strict=True), None
            state = ObserverState.model_validate_json(row["state_json"], strict=True)
            if state.intent_alignment.status is IntentAlignmentStatus.DRIFTED:
                proposal = connection.execute(
                    "SELECT * FROM observer_correction_proposals WHERE run_id=?", (run_id,),
                ).fetchone()
                if proposal is None:
                    proposal_id = hashlib.sha256(
                        f"observer-correction:{run_id}:{row['revision']}".encode("utf-8")
                    ).hexdigest()
                    created = datetime.now().astimezone()
                    prompt = (
                        "重新聚焦用户原始问题：" + state.user_problem + "。"
                        "避免继续执行与该目标无关的动作。"
                    )
                    connection.execute(
                        "INSERT INTO observer_correction_proposals(proposal_id,run_id,status,"
                        "proposed_prompt,expires_at,revision,created_at) "
                        "VALUES(?,?,'pending',?,?,0,?)",
                        (
                            proposal_id, run_id, prompt,
                            (created + timedelta(seconds=timeout_seconds)).isoformat(timespec="microseconds"),
                            created.isoformat(timespec="microseconds"),
                        ),
                    )
                    proposal = connection.execute(
                        "SELECT * FROM observer_correction_proposals WHERE proposal_id=?",
                        (proposal_id,),
                    ).fetchone()
                # The Run-side Observer lifecycle is complete even while the
                # independent Core correction proposal remains pending.  The
                # proposal table, not observer_instances, owns that workflow.
                connection.execute(
                    "UPDATE observer_instances SET status='finalized',"
                    "revision=revision+1,updated_at=? WHERE run_id=? AND status='active'",
                    (now_iso(), run_id),
                )
                connection.commit()
                return None, dict(proposal)
            built = self._insert_evidence_in_transaction(connection, row, state)
            connection.execute(
                "UPDATE observer_instances SET status='finalized',revision=revision+1,updated_at=? "
                "WHERE run_id=?", (now_iso(), run_id),
            )
            connection.commit()
            return built, None

    def decide_correction(
        self, proposal_id: str, *, expected_revision: int, action: str,
        actor: str, edited_prompt: str | None = None, reason: str = "",
    ) -> ObserverEvidence:
        if action not in {"adopt", "edit", "reject"}:
            raise ValueError("Observer correction action must be adopt, edit or reject")
        if action == "edit" and not (edited_prompt or "").strip():
            raise ValueError("Edited correction prompt is required")
        timestamp = now_iso()
        status = {"adopt": "adopted", "edit": "edited", "reject": "rejected"}[action]
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                "SELECT * FROM observer_correction_proposals WHERE proposal_id=?", (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal["status"] != "pending":
                desired = {
                    "adopt": "adopted", "edit": "edited", "reject": "rejected",
                }[action]
                if proposal["status"] != desired:
                    raise RuntimeError(
                        "Observer correction already has a conflicting decision: "
                        + str(proposal["status"])
                    )
                evidence = connection.execute(
                    "SELECT evidence_json FROM observer_evidence WHERE run_id=?", (proposal["run_id"],),
                ).fetchone()
                if evidence is None:
                    raise RuntimeError("Observer correction is terminal without Evidence")
                connection.commit()
                return ObserverEvidence.model_validate_json(evidence["evidence_json"], strict=True)
            if int(proposal["revision"]) != expected_revision:
                raise RuntimeError("Observer correction revision conflict")
            changed = connection.execute(
                "UPDATE observer_correction_proposals SET status=?,edited_prompt=?,actor=?,"
                "reason=?,revision=revision+1,decided_at=? WHERE proposal_id=? "
                "AND status='pending' AND revision=?",
                (status, edited_prompt, actor, reason, timestamp, proposal_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Observer correction decision CAS conflict")
            instance = connection.execute(
                "SELECT * FROM observer_instances WHERE run_id=?", (proposal["run_id"],),
            ).fetchone()
            state = ObserverState.model_validate_json(instance["state_json"], strict=True)
            selected_prompt = (
                str(edited_prompt) if action == "edit"
                else str(proposal["proposed_prompt"]) if action == "adopt" else None
            )
            built = self._insert_evidence_in_transaction(
                connection, instance, state,
                user_correction=reason or action,
                adopted_correction_prompt=selected_prompt,
            )
            connection.execute(
                "UPDATE observer_instances SET status='finalized',revision=revision+1,updated_at=? "
                "WHERE run_id=?", (timestamp, proposal["run_id"]),
            )
            connection.commit()
            return built

    def expire_due(self) -> tuple[str, ...]:
        expired: list[str] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT proposal_id,revision FROM observer_correction_proposals "
                "WHERE status='pending' AND expires_at<=?", (now_iso(),),
            ).fetchall()
        for row in rows:
            self._timeout(str(row["proposal_id"]), int(row["revision"]))
            expired.append(str(row["proposal_id"]))
        return tuple(expired)

    def status(self, run_id: str) -> dict[str, Any]:
        row = self.instance(run_id)
        if row is None:
            raise KeyError(run_id)
        state = ObserverState.model_validate_json(row["state_json"], strict=True)
        with self._connect() as connection:
            proposal = connection.execute(
                "SELECT * FROM observer_correction_proposals WHERE run_id=?", (run_id,),
            ).fetchone()
            loops = self._tool_loops_in(connection, run_id)
            terminal_row = connection.execute(
                "SELECT visible_event_json FROM observer_visible_events "
                "WHERE run_id=? AND visible_event_json IS NOT NULL "
                "ORDER BY stream_sequence DESC LIMIT 1", (run_id,),
            ).fetchone()
        terminal_event_type = None
        if terminal_row is not None:
            latest = VisibleObserverEvent.model_validate_json(
                terminal_row["visible_event_json"], strict=True,
            )
            if latest.event_type in _TERMINAL_TYPES:
                terminal_event_type = latest.event_type
        return {
            "run_id": run_id,
            "status": row["status"],
            "last_event_offset": row["last_event_offset"],
            "generation_id": row["generation_id"],
            "observer_plugin_version": row["observer_plugin_version"],
            "state_schema_version": row["state_schema_version"],
            "runtime_role": row["runtime_role"],
            "agent_role": row["agent_role"],
            "runtime_profile": row["runtime_profile"],
            "trigger": row["trigger_name"],
            "state": state.model_dump(mode="json"),
            "progress_markdown": render_observer_progress(
                state,
                terminal_event_type=terminal_event_type,
                observer_failed=row["status"] == "failed",
            ),
            "tool_loops": [item.model_dump(mode="json") for item in loops],
            "correction_proposal": dict(proposal) if proposal is not None else None,
        }

    def finalized_evidence(self, *, unconsumed_only: bool = False) -> tuple[ObserverEvidence, ...]:
        sql = "SELECT evidence_json FROM observer_evidence"
        if unconsumed_only:
            sql += " WHERE consumed_at IS NULL"
        sql += " ORDER BY finalized_at"
        with self._connect() as connection:
            return tuple(
                ObserverEvidence.model_validate_json(row["evidence_json"], strict=True)
                for row in connection.execute(sql).fetchall()
            )

    def coding_context_summary(self, *, limit: int = 12) -> str:
        """Return a bounded, visible-only summary for a new isolated coding session."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT evidence_json FROM observer_evidence "
                "WHERE runtime_profile LIKE 'harness:%' "
                "ORDER BY finalized_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items: list[str] = []
        for row in reversed(rows):
            evidence = ObserverEvidence.model_validate_json(
                row["evidence_json"], strict=True,
            )
            completed = "; ".join(evidence.completed_tasks[-5:])
            summary = completed or evidence.visible_execution_summary or "no visible result"
            items.append(
                f"[{evidence.runtime_profile}/{evidence.trigger}] "
                f"{evidence.user_problem[:500]} -> {summary[:1000]}"
            )
        return "\n".join(items)[-6000:]

    def create_skill_candidates(self, *, minimum_evidence: int = 3) -> tuple[dict[str, Any], ...]:
        evidence = self.finalized_evidence(unconsumed_only=True)
        grouped: dict[tuple[str, str], list[ObserverEvidence]] = {}
        for item in evidence:
            grouped.setdefault((item.runtime_profile, item.trigger), []).append(item)
        created: list[dict[str, Any]] = []
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for (profile, trigger), items in sorted(grouped.items()):
                if len(items) < minimum_evidence:
                    continue
                selected = items[:50]
                evidence_ids = tuple(item.evidence_id for item in selected)
                digest = hashlib.sha256(canonical_json(evidence_ids).encode("utf-8")).hexdigest()
                candidate_id = "observer-skill-" + digest
                existing = connection.execute(
                    "SELECT candidate_json FROM observer_skill_candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if existing is not None:
                    continue
                name = "observer-evolved-" + re.sub(r"[^a-z0-9]+", "-", profile.lower()).strip("-") + "-" + digest[:8]
                steps = _stable_steps(selected)
                if not steps:
                    # Evidence remains unconsumed until a stable pattern is
                    # supported by at least two distinct Runs.
                    continue
                candidate = {
                    "candidate_id": candidate_id,
                    "name": name,
                    "description": f"Stable workflow distilled from {len(selected)} finalized Observer Evidence records for {profile}.",
                    "runtime_profile": profile,
                    "trigger": trigger,
                    "evidence_ids": evidence_ids,
                    "validation": {"minimum_evidence": minimum_evidence, "profile_isolated": True},
                    "skill_markdown": _skill_markdown(name, profile, steps),
                }
                connection.execute(
                    "INSERT INTO observer_skill_candidates(candidate_id,runtime_profile,trigger_name,"
                    "evidence_ids_json,candidate_json,status,revision,created_at) "
                    "VALUES(?,?,?,?,?,'awaiting_approval',0,?)",
                    (
                        candidate_id, profile, trigger, canonical_json(evidence_ids),
                        canonical_json(candidate), now_iso(),
                    ),
                )
                connection.executemany(
                    "UPDATE observer_evidence SET consumed_at=? WHERE evidence_id=? AND consumed_at IS NULL",
                    [(now_iso(), item) for item in evidence_ids],
                )
                created.append(candidate)
            connection.commit()
        return tuple(created)

    def skill_candidates(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT candidate_json,status,revision,created_at,decided_at "
                "FROM observer_skill_candidates ORDER BY created_at DESC",
            ).fetchall()
        return tuple({
            **json.loads(str(row["candidate_json"])),
            "status": row["status"], "revision": row["revision"],
            "created_at": row["created_at"], "decided_at": row["decided_at"],
        } for row in rows)

    def decide_skill_candidate(
        self, candidate_id: str, *, expected_revision: int, approved: bool,
    ) -> dict[str, Any]:
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM observer_skill_candidates WHERE candidate_id=?", (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            if row["status"] != "awaiting_approval":
                existing_status = str(row["status"])
                compatible = (
                    approved and existing_status in {"approved", "published"}
                ) or (not approved and existing_status == "rejected")
                if not compatible:
                    raise RuntimeError(
                        f"Observer Skill Candidate already decided as {existing_status}"
                    )
                connection.commit()
                return {
                    **json.loads(str(row["candidate_json"])),
                    "status": existing_status,
                    "revision": int(row["revision"]),
                }
            if int(row["revision"]) != expected_revision:
                raise RuntimeError("Observer Skill Candidate revision conflict")
            status = "approved" if approved else "rejected"
            changed = connection.execute(
                "UPDATE observer_skill_candidates SET status=?,revision=revision+1,decided_at=? "
                "WHERE candidate_id=? AND status='awaiting_approval' AND revision=?",
                (status, now_iso(), candidate_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Observer Skill Candidate decision CAS conflict")
            connection.commit()
            return {**json.loads(str(row["candidate_json"])), "status": status}

    def mark_skill_published(self, candidate_id: str) -> None:
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM observer_skill_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            if row["status"] == "published":
                connection.commit()
                return
            changed = connection.execute(
                "UPDATE observer_skill_candidates SET status='published',revision=revision+1,"
                "decided_at=? WHERE candidate_id=? AND status='approved'",
                (now_iso(), candidate_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Observer Skill Candidate is not approved")
            connection.commit()

    def active_runs(self) -> tuple[str, ...]:
        with self._connect() as connection:
            return tuple(str(row[0]) for row in connection.execute(
                "SELECT run_id FROM observer_instances WHERE status='active' ORDER BY created_at",
            ).fetchall())

    def health(self) -> dict[str, int]:
        """Small control-plane projection; never includes observed content."""
        with self._connect() as connection:
            values = {
                "observer_active_instances": "SELECT COUNT(*) FROM observer_instances WHERE status='active'",
                "observer_failed_instances": "SELECT COUNT(*) FROM observer_instances WHERE status='failed'",
                "observer_pending_corrections": "SELECT COUNT(*) FROM observer_correction_proposals WHERE status='pending'",
                "observer_finalized_evidence": "SELECT COUNT(*) FROM observer_evidence",
                "observer_pending_skill_candidates": "SELECT COUNT(*) FROM observer_skill_candidates WHERE status='awaiting_approval'",
            }
            return {
                name: int(connection.execute(statement).fetchone()[0])
                for name, statement in values.items()
            }

    def _timeout(self, proposal_id: str, expected_revision: int) -> None:
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                "SELECT * FROM observer_correction_proposals WHERE proposal_id=?", (proposal_id,),
            ).fetchone()
            if proposal is None or proposal["status"] != "pending":
                connection.commit()
                return
            if int(proposal["revision"]) != expected_revision:
                connection.commit()
                return
            connection.execute(
                "UPDATE observer_correction_proposals SET status='timed_out',actor='core:timeout',"
                "reason='observer_correction_timeout',revision=revision+1,decided_at=? "
                "WHERE proposal_id=? AND revision=?", (now_iso(), proposal_id, expected_revision),
            )
            instance = connection.execute(
                "SELECT * FROM observer_instances WHERE run_id=?", (proposal["run_id"],),
            ).fetchone()
            state = ObserverState.model_validate_json(instance["state_json"], strict=True)
            self._insert_evidence_in_transaction(
                connection, instance, state, user_correction="timeout_rejected",
            )
            connection.execute(
                "UPDATE observer_instances SET status='finalized',revision=revision+1,updated_at=? "
                "WHERE run_id=?", (now_iso(), proposal["run_id"]),
            )
            connection.commit()

    def _insert_evidence_in_transaction(
        self, connection: sqlite3.Connection, instance: sqlite3.Row,
        state: ObserverState, *, user_correction: str | None = None,
        adopted_correction_prompt: str | None = None,
    ) -> ObserverEvidence:
        run_id = str(instance["run_id"])
        loops = self._tool_loops_in(connection, run_id)
        finalized_at = now_iso()
        evidence_id = hashlib.sha256(
            f"observer-evidence:{run_id}:{instance['generation_id']}".encode("utf-8")
        ).hexdigest()
        # Skill-evolution evidence is deliberately rule-derived from durable,
        # user-visible events. LLM-maintained progress is presentation state,
        # not authoritative execution evidence.
        completed_tasks = self._rule_completed_tasks_in(connection, run_id)
        summary = "; ".join(completed_tasks[-10:])
        evidence = ObserverEvidence(
            evidence_id=evidence_id, run_id=run_id, session_id=instance["session_id"],
            generation_id=str(instance["generation_id"]),
            observer_plugin_id=str(instance["observer_plugin_id"]),
            observer_plugin_version=str(instance["observer_plugin_version"]),
            state_schema_version=int(instance["state_schema_version"]),
            runtime_role=str(instance["runtime_role"]), agent_role=str(instance["agent_role"]),
            runtime_profile=str(instance["runtime_profile"]), trigger=str(instance["trigger_name"]),
            user_problem=state.user_problem, completed_tasks=completed_tasks,
            visible_execution_summary=summary,
            user_correction=user_correction,
            final_intent_alignment=state.intent_alignment,
            adopted_correction_prompt=adopted_correction_prompt,
            tool_loops=loops, finalized_at=finalized_at,
        )
        connection.execute(
            "INSERT INTO observer_evidence(evidence_id,run_id,runtime_profile,trigger_name,"
            "evidence_json,finalized_at) VALUES(?,?,?,?,?,?)",
            (
                evidence_id, run_id, evidence.runtime_profile, evidence.trigger,
                evidence.model_dump_json(), finalized_at,
            ),
        )
        return evidence

    @staticmethod
    def _rule_completed_tasks_in(
        connection: sqlite3.Connection, run_id: str,
    ) -> tuple[str, ...]:
        completed: list[str] = []
        for row in connection.execute(
            "SELECT visible_event_json FROM observer_visible_events "
            "WHERE run_id=? AND visible=1 ORDER BY stream_sequence",
            (run_id,),
        ).fetchall():
            if not row["visible_event_json"]:
                continue
            event = VisibleObserverEvent.model_validate_json(
                str(row["visible_event_json"]), strict=True,
            )
            item = ""
            if event.event_type == "tool_completed" and event.tool_name:
                item = f"工具 {event.tool_name}：{event.tool_status or 'completed'}"
            elif event.event_type in {
                "final", "run_completed", "run_failed", "run_cancelled", "run_interrupted",
            } and event.content:
                item = _limit(event.content, limit=240)
            if item and (not completed or completed[-1] != item):
                completed.append(item)
        return tuple(completed[-20:])

    @staticmethod
    def _append_tool_loop_in_transaction(
        connection: sqlite3.Connection, run_id: str,
        event: GatewayEventEnvelope, timestamp: str,
    ) -> None:
        loop = event.payload.get("loop")
        name = event.payload.get("name")
        execution = event.payload.get("execution")
        if not isinstance(loop, int) or loop < 1 or not isinstance(name, str):
            return
        if execution not in {"serial", "parallel"}:
            execution = "serial"
        row = connection.execute(
            "SELECT execution,tools_json,revision FROM observer_tool_loops WHERE run_id=? AND loop=?",
            (run_id, loop),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO observer_tool_loops(run_id,loop,execution,tools_json,revision,updated_at) "
                "VALUES(?,?,?,?,0,?)", (run_id, loop, execution, canonical_json([name]), timestamp),
            )
            return
        tools = list(json.loads(str(row["tools_json"])))
        tools.append(name)
        mode = str(row["execution"])
        if mode != execution:
            mode = "mixed"
        connection.execute(
            "UPDATE observer_tool_loops SET execution=?,tools_json=?,revision=revision+1,updated_at=? "
            "WHERE run_id=? AND loop=? AND revision=?",
            (mode, canonical_json(tools), timestamp, run_id, loop, int(row["revision"])),
        )

    @staticmethod
    def _tool_loops_in(connection: sqlite3.Connection, run_id: str) -> tuple[ObserverToolLoop, ...]:
        return tuple(ObserverToolLoop(
            loop=int(row["loop"]), execution=str(row["execution"]),
            tools=tuple(json.loads(str(row["tools_json"]))),
        ) for row in connection.execute(
            "SELECT * FROM observer_tool_loops WHERE run_id=? ORDER BY loop", (run_id,),
        ).fetchall())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _write_connection(self):
        guard = (
            self.write_gate.database_transaction()
            if self.write_gate is not None else nullcontext()
        )
        with guard:
            with self._connect() as connection:
                yield connection


class GatewayObserverService:
    """Consume canonical visible events with an immutable Generation plugin."""

    def __init__(
        self, *, event_store: EventStore, state_store: ObserverStateStore,
        gateway_store: Any, resource_manager: Any, config: RuntimeConfig,
        timeout_seconds: int = 60,
        plugin_timeout_seconds: float = 2.0,
        model_timeout_seconds: float = 60.0,
        provider_factory: ObserverProviderFactory | None = None,
    ) -> None:
        self.event_store = event_store
        self.state_store = state_store
        self.gateway_store = gateway_store
        self.resource_manager = resource_manager
        self.timeout_seconds = timeout_seconds
        self.plugin_timeout_seconds = plugin_timeout_seconds
        self.model_timeout_seconds = model_timeout_seconds
        self.model_runtime = ObserverAgentRuntime(
            config, provider_factory=provider_factory,
        )
        self._plugins: dict[tuple[str, str, str], ObserverPlugin] = {}
        self._timeout_task: asyncio.Task[None] | None = None
        self._maintenance_epoch: int | None = None
        self._consume_lock = threading.RLock()
        self._callback_worker = _ObserverCallbackWorker()

    async def start(self) -> None:
        if self._timeout_task is None:
            self._timeout_task = asyncio.create_task(
                self._expire_loop(), name="observer-correction-timeouts",
            )

    async def close(self) -> None:
        task, self._timeout_task = self._timeout_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._plugins.clear()
        self._callback_worker.close()

    async def quiesce(self, maintenance_epoch: int):
        from backup.models import QuiesceResult

        self._maintenance_epoch = maintenance_epoch
        await asyncio.to_thread(self._consume_barrier)
        return QuiesceResult(
            participant="observer", maintenance_epoch=maintenance_epoch,
            acknowledged=True, safe_boundary="observer_idle",
        )

    async def resume(self, maintenance_epoch: int) -> None:
        if self._maintenance_epoch == maintenance_epoch:
            self._maintenance_epoch = None

    def _consume_barrier(self) -> None:
        with self._consume_lock:
            pass

    def observe_event(self, event_id: str) -> tuple[ObserverOutputEvent, ...]:
        with self._consume_lock:
            if self._maintenance_epoch is not None:
                return ()
            return self._observe_event(event_id)

    @staticmethod
    def accepts_event_type(event_type: str) -> bool:
        """Return whether this durable milestone should schedule Observer work."""
        return event_type in _OBSERVER_MODEL_TYPES

    def _observe_event(self, event_id: str) -> tuple[ObserverOutputEvent, ...]:
        source = self.event_store.read_canonical(event_id).envelope
        run = self.gateway_store.run(source.run_id)
        profile = _profile_for_run(run)
        generation_id = self.resource_manager.referenced_generation(
            owner_kind="run", owner_id=run.run_id,
        )
        existing = self.state_store.instance(run.run_id)
        if existing is not None and existing["status"] != "active":
            return ()
        if generation_id is None and existing is not None:
            generation_id = str(existing["generation_id"])
        snapshot = self.resource_manager.snapshot(profile, generation_id=generation_id)
        contributions = tuple(
            item for item in snapshot.contributions
            if item.kind is RuntimeContributionKind.OBSERVER
        )
        if not contributions:
            return ()
        contribution = contributions[0]
        plugin = self._plugin(
            snapshot, contribution.plugin_id, contribution.plugin_version, run.run_id,
        )
        last_offset = int(existing["last_event_offset"]) if existing is not None else 0
        previous = (
            self._restore_state(existing, plugin)
            if existing is not None else plugin.initial_state(run.task)
        )
        outputs: list[ObserverOutputEvent] = []
        for record in self.event_store.read_stream(
            run.run_id, after_sequence=last_offset,
        ):
            event = record.envelope
            if int(event.stream_sequence or event.sequence) > int(source.stream_sequence or source.sequence):
                break
            visible = VisibleEventProjection.project(event)
            should_reduce = visible is not None and event.type in _OBSERVER_MODEL_TYPES
            updated = (
                self._reduce(
                    plugin, previous, visible, terminal=event.type in _TERMINAL_TYPES,
                )
                if should_reduce else previous
            )
            committed = self.state_store.commit_event(
                run=run, generation_id=snapshot.generation_id, plugin=plugin,
                runtime_profile=profile, trigger=_trigger_for_run(run), event=event,
                visible=visible, previous=previous, updated=updated,
            )
            previous = updated
            # Terminal events finalize Observer evidence below.  Do not append a
            # second progress event after the Run terminal marker: the normal
            # Gateway timeline must retain its terminal ordering, while the
            # durable Observer projection remains queryable through its own API.
            if committed and should_reduce and event.type not in _TERMINAL_TYPES:
                outputs.append(ObserverOutputEvent("observer_progress", {
                    "state": updated.model_dump(mode="json"),
                    "progress_markdown": render_observer_progress(updated),
                    "source_event_id": event.event_id,
                    "source_sequence": visible.stream_sequence,
                    "observer_plugin_version": plugin.plugin_version,
                    "state_schema_version": plugin.state_schema_version,
                }))
            if committed and event.type in _TERMINAL_TYPES:
                evidence, proposal = self.state_store.finalize_or_propose(
                    run.run_id, timeout_seconds=self.timeout_seconds,
                )
                if proposal is not None:
                    outputs.append(ObserverOutputEvent("observer_correction_proposed", {
                        "proposal_id": proposal["proposal_id"],
                        "run_id": run.run_id,
                        "proposed_prompt": proposal["proposed_prompt"],
                        "expires_at": proposal["expires_at"],
                        "revision": proposal["revision"],
                    }))
                # Finalized evidence is Level-1 Observer state, not a chat
                # timeline item.  Dream reads it from ObserverStateStore.
                self._plugins.pop(
                    (run.run_id, snapshot.generation_id, contribution.plugin_id), None,
                )
        # commit_event and finalization intentionally use separate transactions.
        # If a process stopped between them, the terminal offset is already
        # durable.  Reconcile that exact terminal fact without replaying reduce().
        current = self.state_store.instance(run.run_id)
        source_sequence = int(source.stream_sequence or source.sequence)
        if (
            source.type in _TERMINAL_TYPES
            and current is not None
            and current["status"] == "active"
            and int(current["last_event_offset"]) >= source_sequence
        ):
            _, proposal = self.state_store.finalize_or_propose(
                run.run_id, timeout_seconds=self.timeout_seconds,
            )
            if proposal is not None:
                outputs.append(ObserverOutputEvent("observer_correction_proposed", {
                    "proposal_id": proposal["proposal_id"],
                    "run_id": run.run_id,
                    "proposed_prompt": proposal["proposed_prompt"],
                    "expires_at": proposal["expires_at"],
                    "revision": proposal["revision"],
                }))
            self._plugins.pop(
                (run.run_id, snapshot.generation_id, contribution.plugin_id), None,
            )
        self.resource_manager.report_success(
            generation_id=snapshot.generation_id,
            plugin_id=contribution.plugin_id,
            profile=profile,
        )
        return tuple(outputs)

    def recover(self) -> tuple[ObserverOutputEvent, ...]:
        outputs: list[ObserverOutputEvent] = []
        if self._maintenance_epoch is not None:
            return ()
        self.state_store.expire_due()
        for run_id in self.state_store.active_runs():
            maximum = self.event_store.max_sequence(run_id)
            if maximum <= 0:
                continue
            record = self.event_store.read_stream(run_id, after_sequence=maximum - 1, limit=1)
            if record:
                try:
                    outputs.extend(self.observe_event(record[0].event_id))
                except Exception as exc:
                    self.record_failure(run_id, exc)
        return tuple(outputs)

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self._maintenance_epoch is not None:
                continue
            # Direct Gateway workloads (Harness, Dream, maintenance) do not run
            # through ReactLoop._emit. Consume their canonical events by durable
            # per-Run offset so call-site coverage is never the authority.
            await asyncio.to_thread(self.recover)

    def decide_correction(self, *args: Any, **kwargs: Any) -> ObserverEvidence:
        return self.state_store.decide_correction(*args, **kwargs)

    def record_failure(self, run_id: str, error: BaseException) -> None:
        """Attribute implementation failure without ever failing the Main Runtime."""
        existing = self.state_store.instance(run_id)
        if existing is not None:
            self.state_store.mark_failed(run_id)
            generation_id = str(existing["generation_id"])
            plugin_id = str(existing["observer_plugin_id"])
            profile: RuntimeProfile | str = str(existing["runtime_profile"])
        else:
            # Loading can fail before the first Observer state row exists.  The
            # Run's immutable Generation binding still provides exact fault
            # attribution and permits normal generation quarantine/rollback.
            run = self.gateway_store.run(run_id)
            profile = _profile_for_run(run)
            generation_id = self.resource_manager.referenced_generation(
                owner_kind="run", owner_id=run_id,
            )
            if generation_id is None:
                return
            plugin_id = "builtin.observer"
        self.resource_manager.report_failure(
            generation_id=generation_id,
            plugin_id=plugin_id,
            profile=profile,
            failure_kind=(
                "provider_failure" if isinstance(error, ObserverModelFailure)
                else "plugin_timeout" if isinstance(error, ObserverPluginTimeout)
                else "plugin_exception"
            ),
            error=error,
            actor="observer",
        )

    def _plugin(
        self, snapshot: Any, plugin_id: str, plugin_version: str, run_id: str,
    ) -> ObserverPlugin:
        key = (run_id, snapshot.generation_id, plugin_id)
        cached = self._plugins.get(key)
        if cached is not None:
            return cached
        path = snapshot.source_root / "observer_plugins" / "default.py"
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Observer Generation has no immutable default plugin")
        module_name = f"yy_observer_{snapshot.generation_id[:16]}_{plugin_id.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load Observer plugin Generation")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = module.create_observer_plugin()
        if plugin.plugin_id != plugin_id or plugin.plugin_version != plugin_version:
            raise RuntimeError("Observer plugin descriptor identity mismatch")
        self._plugins[key] = plugin
        return plugin

    def _reduce(
        self, plugin: ObserverPlugin, previous: ObserverState,
        visible: VisibleObserverEvent, *, terminal: bool = False,
    ) -> ObserverState:
        try:
            messages = self._invoke_callback(
                lambda: plugin.model_messages(
                    previous, visible, terminal=terminal,
                ),
                timeout=self.plugin_timeout_seconds,
                timeout_error=ObserverPluginTimeout(
                    f"Observer plugin exceeded {self.plugin_timeout_seconds:g} seconds"
                ),
            )
            if not isinstance(messages, tuple) or not messages:
                raise TypeError("Observer plugin returned invalid model messages")
            if any(
                not isinstance(item, dict)
                or item.get("role") not in {"system", "user", "assistant"}
                or not isinstance(item.get("content"), str)
                for item in messages
            ):
                raise TypeError("Observer plugin returned invalid model messages")
        except ObserverPluginTimeout:
            raise
        except Exception:
            raise

        try:
            raw = self._invoke_callback(
                lambda: asyncio.run(self.model_runtime.complete(messages)),
                timeout=self.model_timeout_seconds,
                timeout_error=ObserverModelTimeout(
                    f"Observer model exceeded {self.model_timeout_seconds:g} seconds"
                ),
            )
        except ObserverModelFailure:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ObserverModelFailure(str(exc) or type(exc).__name__) from exc
        if not isinstance(raw, str):
            raise ObserverModelOutputError("Observer model returned non-text output")
        try:
            updated = self._invoke_callback(
                lambda: plugin.parse_state(raw, previous),
                timeout=self.plugin_timeout_seconds,
                timeout_error=ObserverPluginTimeout(
                    f"Observer plugin exceeded {self.plugin_timeout_seconds:g} seconds"
                ),
            )
        except ObserverPluginTimeout:
            raise
        except Exception as exc:
            raise ObserverModelOutputError(
                str(exc) or "Observer model returned invalid state JSON"
            ) from exc
        if not isinstance(updated, ObserverState):
            raise ObserverModelOutputError("Observer plugin returned invalid state")
        return updated

    def _invoke_callback(
        self, callback: Callable[[], object], *, timeout: float,
        timeout_error: RuntimeError,
    ) -> object:
        worker = self._callback_worker
        try:
            return worker.invoke(callback, timeout=timeout)
        except _ObserverCallbackTimeout as exc:
            # Python cannot safely kill a stuck in-process callback. Retire this
            # daemon worker so later state updates are never queued behind it.
            worker.close()
            self._callback_worker = _ObserverCallbackWorker()
            raise timeout_error from exc

    @staticmethod
    def _restore_state(row: dict[str, Any], plugin: ObserverPlugin) -> ObserverState:
        version = int(row["state_schema_version"])
        if version == plugin.state_schema_version:
            return ObserverState.model_validate_json(str(row["state_json"]), strict=True)
        return plugin.migrate_state(json.loads(str(row["state_json"])), version)


def _profile_for_run(run: RunRecord) -> RuntimeProfile:
    workload = run.workload_kind
    if workload == "cron":
        return RuntimeProfile.CRON
    if workload in {"dream", "dream_backfill", "dream_rollback"}:
        return RuntimeProfile.DREAM
    if workload == "maintenance":
        return RuntimeProfile.MAINTENANCE
    if workload in {"code_session_start", "code_turn", "code_finalize", "code_abort"}:
        return RuntimeProfile.HARNESS_MANUAL
    if workload == "harness_dream":
        return RuntimeProfile.HARNESS_DREAM
    if workload == "harness_evolution":
        lowered = run.task.lower()
        return (
            RuntimeProfile.HARNESS_CAPABILITY
            if "capability" in lowered else RuntimeProfile.HARNESS_ERROR
        )
    return RuntimeProfile.INTERACTIVE


def _trigger_for_run(run: RunRecord) -> str:
    if run.workload_kind == "harness_evolution":
        return "capability" if "capability" in run.task.lower() else "error"
    return {
        "chat": "chat",
        "cron": "cron",
        "code_session_start": "manual",
        "code_turn": "manual",
        "code_finalize": "manual",
        "code_abort": "manual",
        "harness_dream": "dream",
        "dream": "dream",
        "dream_backfill": "dream",
        "dream_rollback": "dream",
        "maintenance": "maintenance",
    }.get(run.workload_kind, run.workload_kind)


def _runtime_role(profile: RuntimeProfile) -> str:
    return "harness" if profile.value.startswith("harness:") else profile.value


def _agent_role(profile: RuntimeProfile) -> str:
    return "main" if profile is RuntimeProfile.INTERACTIVE else profile.value


def _limit(value: str, limit: int = 4000) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _stable_steps(evidence: Iterable[ObserverEvidence]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for item in evidence:
        supported_by_run: set[str] = set()
        for task in item.completed_tasks:
            normalized = " ".join(task.split())[:240]
            if normalized:
                supported_by_run.add(normalized)
        for loop in item.tool_loops:
            if loop.tools:
                normalized = f"按 {loop.execution} 顺序调用：" + " → ".join(loop.tools)
                supported_by_run.add(normalized)
        for normalized in supported_by_run:
            counts[normalized] = counts.get(normalized, 0) + 1
    repeated = [item for item, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])) if count >= 2]
    return tuple(repeated[:12])


def _skill_markdown(name: str, profile: str, steps: tuple[str, ...]) -> str:
    body = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1))
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Stable workflow learned from repeated finalized Observer Evidence for {profile}.\n"
        "license: MIT\n"
        f"metadata:\n  runtime_profile: {profile}\n"
        "---\n\n"
        "# Observer-evolved workflow\n\n"
        "Use this only within the declared runtime profile. Re-check the current user request; "
        "historical evidence is guidance, not authority.\n\n"
        f"{body}\n"
    )


__all__ = [
    "GatewayObserverService", "ObserverAgentRuntime", "ObserverModelFailure",
    "ObserverModelOutputError", "ObserverModelTimeout", "ObserverOutputEvent",
    "ObserverStateStore", "ObserverPluginTimeout", "VisibleEventProjection",
]
