"""Gateway bridge for the CAPABILITY Harness trigger.

It deliberately calls the Harness engine directly instead of creating a nested /code workload.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from Agent import RuntimeConfig, RuntimeFailure
from gateway.audit import AuditSanitizer
from memory import MemoryStore
from gateway.harness_dream import (
    DreamEvolutionContext,
    HarnessDreamChangeScanner,
    HarnessDreamChangeSet,
)


def _load_harness(source_root: Path) -> ModuleType:
    path = source_root.resolve() / "harness-evolution" / "harness.py"
    spec = importlib.util.spec_from_file_location("yy_harness_evolution_service", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Harness implementation: {path}")
    existing = sys.modules.get("yy_harness_evolution_service")
    if existing is not None:
        return existing
    module = importlib.util.module_from_spec(spec)
    sys.modules["yy_harness_evolution_service"] = module
    spec.loader.exec_module(module)
    return module


class GatewayHarnessEvolutionService:
    """The only Gateway bridge for ERROR and CAPABILITY Harness invocations."""

    def __init__(self, config: RuntimeConfig, *, store=None, state_controller=None) -> None:
        self.config = config
        self.store = store
        self.state_controller = state_controller
        self.source_root = (config.coding_source_root or Path(__file__).resolve().parents[1]).resolve()
        self.module = _load_harness(self.source_root)
        self.proposals_root = (
            config.agent_root / ".yy" / "harness-evolution" / "proposals"
        ).resolve()
        self.dream_scanner = HarnessDreamChangeScanner(
            config.agent_root, self.source_root, config.dream_timezone,
        )

    async def evolve_capability(
        self, *, operation_id: str, task: str, capability_gap: dict[str, Any],
    ) -> dict[str, Any]:
        origin = self._origin_for_operation(operation_id, trigger_evidence={
            "capability_gap": capability_gap,
        })
        request = self.module.HarnessEvolutionRequest(
            task=task,
            config=self.config,
            trigger="capability",
            target="tool",
            source_root=self.source_root,
            agent_root=self.config.agent_root,
            operation_id=operation_id,
            capability_gap=self.module.CapabilityGap.model_validate({
                **capability_gap,
                "acceptance_criteria": tuple(capability_gap.get("acceptance_criteria", ())),
                "safety_constraints": tuple(capability_gap.get("safety_constraints", ())),
            }, strict=True),
            origin=self.module.HarnessOriginContext.model_validate(origin, strict=True),
            max_attempts=4,
            merge_policy="immediate",
        )
        result = await self.module.HarnessEvolutionEngine.for_config(self.config).run(request)
        return result.model_dump(mode="json")

    def _origin_for_operation(
        self, operation_id: str, *, trigger_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if self.store is None or self.state_controller is None:
            raise RuntimeError("Harness Gateway service is not bound to durable state")
        operation = self.state_controller.operation(operation_id)
        run = self.store.run(operation.run_id)
        records: list[dict[str, Any]] = []
        if run.session_id:
            project = self.store.project(run.project_id)
            memory = MemoryStore(
                self.config.memory_dir,
                workspace_root=Path(project.path),
                agent_root=self.config.agent_root,
            )
            if memory.has_session(run.session_id):
                records = list(memory.session_records(run.session_id))
        canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record_ids = tuple(
            str(record.get("record_id")) for record in records if record.get("record_id")
        )
        summary_lines = []
        for record in records[-8:]:
            if record.get("role") not in {"user", "assistant"}:
                continue
            content = str(record.get("content") or "").strip()
            if content:
                summary_lines.append(f"{record.get('role')}: {content[:1000]}")
        return {
            "origin_project_id": run.project_id,
            "origin_session_id": run.session_id,
            "origin_run_id": run.run_id,
            "session_record_ids": record_ids,
            "session_records_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "context_summary": str(AuditSanitizer.sanitize("\n".join(summary_lines)[-6000:])),
            "trigger_evidence": AuditSanitizer.sanitize(trigger_evidence),
        }

    async def reconcile_capability(self, operation_id: str) -> dict[str, Any]:
        invocation_id = __import__("hashlib").sha256(f"capability:{operation_id}".encode("utf-8")).hexdigest()[:32]
        identity = __import__("hashlib").sha256(str(self.source_root).casefold().encode("utf-8")).hexdigest()[:16]
        path = self.config.agent_root / ".yy" / "harness-evolution" / "invocations" / identity / f"{invocation_id}.jsonl"
        if not path.is_file():
            return {"status": "NOT_APPLIED", "evidence": "no invocation audit"}
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        intent = next((item for item in reversed(records) if item.get("event") == "merge_intent"), None)
        committed = next((item for item in reversed(records) if item.get("event") == "merge_committed"), None)
        if committed:
            expected = str(committed.get("merged_commit") or "")
            intent_expected = str((intent or {}).get("verified_commit") or "")
            head = await self._git("rev-parse", "HEAD")
            branch = await self._git("symbolic-ref", "--quiet", "--short", "HEAD")
            expected_branch = str((intent or {}).get("target_branch") or "")
            # A committed receipt is evidence, not proof by itself. The checked-out target
            # must still be the exact verified commit for this invocation.
            if expected and expected == intent_expected and head == expected and branch == expected_branch:
                return {"status": "COMPLETED", "evidence": {"invocation_id": invocation_id, "merged_commit": expected}}
            return {"status": "UNKNOWN", "evidence": {"invocation_id": invocation_id, "reason": "merge receipt and Git facts disagree", "expected_branch": expected_branch, "head": head}}
        if intent:
            verified = str(intent.get("verified_commit") or "")
            base = str(intent.get("base_commit") or "")
            expected_branch = str(intent.get("target_branch") or "")
            head = await self._git("rev-parse", "HEAD")
            branch = await self._git("symbolic-ref", "--quiet", "--short", "HEAD")
            ancestor = await self._git("merge-base", "--is-ancestor", base, verified)
            if verified and base and head == verified and branch == expected_branch and ancestor == "ok":
                return {
                    "status": "COMPLETED",
                    "evidence": {
                        "invocation_id": invocation_id,
                        "merged_commit": verified,
                        "result_source": "git_reconciled_after_merge_intent",
                    },
                }
            # A candidate/verified commit is not the same as an applied source change. Preserve
            # all evidence and never re-run the non-idempotent Engine from reconcile().
            return {"status": "UNKNOWN", "evidence": {"invocation_id": invocation_id, "merge_intent": intent, "head": head, "branch": branch}}
        return {"status": "UNKNOWN", "evidence": {"invocation_id": invocation_id, "reason": "incomplete invocation audit"}}

    def discover_dream_changes(
        self, selected_date: date, *, cutoff_at: datetime,
    ) -> HarnessDreamChangeSet:
        return self.dream_scanner.scan(selected_date, cutoff_at=cutoff_at)

    async def execute_dream(
        self,
        changeset: HarnessDreamChangeSet,
        *,
        generation: int,
        automatic: bool,
        run_id: str,
    ) -> dict[str, Any]:
        invocation_id = hashlib.sha256(
            f"dream:{changeset.stable_key}:g{generation}".encode("utf-8"),
        ).hexdigest()[:32]
        origin = self._origin_for_run(
            run_id,
            trigger_evidence={
                "entry": "harness_dream",
                "date": changeset.date,
                "changeset_hash": changeset.changeset_hash,
                "generation": generation,
                "automatic": automatic,
            },
        )
        request = self.module.HarnessEvolutionRequest(
            task=f"Conservatively optimize verified Harness changes for {changeset.date}",
            config=self.config,
            trigger="dream",
            target="dream_optimize",
            source_root=self.source_root,
            agent_root=self.config.agent_root,
            invocation_id=invocation_id,
            operation_id=f"harness-dream:{run_id}",
            dream_context=DreamEvolutionContext(
                changeset=changeset, generation=generation, automatic=automatic,
            ),
            origin=self.module.HarnessOriginContext.model_validate(origin, strict=True),
            max_attempts=4,
            merge_policy="immediate",
        )
        result = await self.module.HarnessEvolutionEngine.for_config(self.config).run(request)
        return result.model_dump(mode="json")

    async def reconcile_dream(
        self, changeset: HarnessDreamChangeSet, *, generation: int,
    ) -> dict[str, Any]:
        invocation_id = hashlib.sha256(
            f"dream:{changeset.stable_key}:g{generation}".encode("utf-8"),
        ).hexdigest()[:32]
        path = (
            self.config.agent_root / ".yy" / "harness-evolution" / "invocations" /
            changeset.source_identity / f"{invocation_id}.jsonl"
        )
        if not path.is_file():
            return {"status": "NOT_APPLIED", "evidence": "no DREAM invocation audit"}
        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        intent = next((item for item in reversed(records) if item.get("event") == "merge_intent"), None)
        committed = next((item for item in reversed(records) if item.get("event") == "merge_committed"), None)
        receipt = committed or intent
        if receipt is None:
            return {"status": "UNKNOWN", "evidence": "DREAM audit has no merge boundary"}
        verified = str(receipt.get("verified_commit") or "")
        branch = str(receipt.get("target_branch") or "")
        base = str(receipt.get("base_commit") or "")
        head = await self._git("rev-parse", branch) if branch else ""
        ancestry = await self._git("merge-base", "--is-ancestor", base, verified) if base and verified else ""
        if committed and verified and head == verified and ancestry == "ok":
            return {
                "status": "COMPLETED",
                "evidence": {"invocation_id": invocation_id, "merged_commit": verified},
            }
        if intent and not committed and head == base:
            return {
                "status": "NOT_APPLIED",
                "evidence": {"invocation_id": invocation_id, "expected_head": base},
            }
        return {
            "status": "UNKNOWN",
            "evidence": {"invocation_id": invocation_id, "head": head, "receipt": receipt},
        }

    def propose_error(
        self, *, run_id: str, failure: RuntimeFailure,
    ) -> dict[str, Any] | None:
        """Persist a real RuntimeFailure proposal; this method never executes Harness."""
        if not failure.snapshot_worthy or self.store is None:
            return None
        run = self.store.run(run_id)
        project = self.store.project(run.project_id)
        records: list[dict[str, Any]] = []
        session_file = ""
        if run.session_id:
            memory = MemoryStore(
                self.config.memory_dir, workspace_root=Path(project.path),
                agent_root=self.config.agent_root,
            )
            if memory.has_session(run.session_id):
                records = list(memory.session_records(run.session_id))
                session_file = memory.active_filename(run.session_id)
        writer = self.module.ErrorSnapshotWriter(
            self.config.agent_root / ".yy" / "harness-evolution",
            secrets=tuple(filter(None, (
                self.config.api_key, self.config.web_search_api_key,
                self.config.reference_embedding_api_key,
                self.config.compression_api_key,
            ))),
        )
        snapshot = writer.capture(
            task=run.task, session_id=run.session_id or "",
            failure=failure, session_records=records, session_file=session_file,
        )
        proposal_id = hashlib.sha256(
            f"error-proposal:{run.run_id}:{snapshot.stem}".encode("utf-8")
        ).hexdigest()[:32]
        proposal = {
            "proposal_id": proposal_id,
            "status": "proposed",
            "origin_run_id": run.run_id,
            "origin_project_id": run.project_id,
            "origin_session_id": run.session_id,
            "client_id": run.client_id,
            "task": run.task,
            "failure_category": failure.category,
            "snapshot_path": str(snapshot),
            "created_at": datetime.now().astimezone().isoformat(),
        }
        self._write_proposal(proposal)
        return proposal

    def decide_proposal(self, proposal_id: str, *, confirmed: bool, client_id: str) -> dict[str, Any]:
        proposal = self.proposal(proposal_id)
        if proposal["client_id"] != client_id:
            raise PermissionError("Only the originating client can decide this Harness proposal")
        desired = "confirmed" if confirmed else "rejected"
        if proposal["status"] == desired:
            return proposal
        if proposal["status"] != "proposed":
            raise RuntimeError("Harness proposal already has a conflicting decision")
        proposal.update({
            "status": desired,
            "decided_at": datetime.now().astimezone().isoformat(),
        })
        self._write_proposal(proposal)
        return proposal

    async def execute_error(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.proposal(proposal_id)
        if proposal["status"] not in {"confirmed", "running"}:
            raise RuntimeError("Harness ERROR proposal is not confirmed")
        proposal["status"] = "running"
        self._write_proposal(proposal)
        snapshot = Path(proposal["snapshot_path"])
        writer = self.module.ErrorSnapshotWriter(
            self.config.agent_root / ".yy" / "harness-evolution",
        )
        origin = self._origin_for_run(
            proposal["origin_run_id"],
            trigger_evidence={
                "proposal_id": proposal_id,
                "error_snapshot": str(snapshot),
                "failure_category": proposal["failure_category"],
            },
        )
        request = self.module.HarnessEvolutionRequest(
            task=proposal["task"], config=self.config,
            project_root=self.source_root, incident_id=snapshot.stem,
            snapshot_path=snapshot, trigger="error", target="source_repair",
            source_root=self.source_root, agent_root=self.config.agent_root,
            origin=self.module.HarnessOriginContext.model_validate(origin, strict=True),
            max_attempts=4, merge_policy="immediate",
        )
        runner = self.module.HarnessEvolutionRunner(writer)
        result = await runner.run(request)
        proposal.update({"status": result.status, "result": result.model_dump(mode="json")})
        self._write_proposal(proposal)
        return result.model_dump(mode="json")

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        if not __import__("re").fullmatch(r"[0-9a-f]{32}", proposal_id):
            raise ValueError("Invalid Harness proposal ID")
        path = self.proposals_root / f"{proposal_id}.json"
        if not path.is_file():
            raise KeyError(f"Unknown Harness proposal: {proposal_id}")
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def _origin_for_run(
        self, run_id: str, *, trigger_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if self.store is None:
            raise RuntimeError("Harness Gateway service is not bound to the Gateway store")
        run = self.store.run(run_id)
        records: list[dict[str, Any]] = []
        if run.session_id:
            project = self.store.project(run.project_id)
            memory = MemoryStore(
                self.config.memory_dir, workspace_root=Path(project.path),
                agent_root=self.config.agent_root,
            )
            if memory.has_session(run.session_id):
                records = list(memory.session_records(run.session_id))
        canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "origin_project_id": run.project_id,
            "origin_session_id": run.session_id,
            "origin_run_id": run.run_id,
            "session_record_ids": tuple(
                str(item["record_id"]) for item in records if item.get("record_id")
            ),
            "session_records_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "context_summary": str(AuditSanitizer.sanitize("\n".join(
                f"{item.get('role')}: {str(item.get('content') or '')[:1000]}"
                for item in records[-8:] if item.get("role") in {"user", "assistant"}
            )[-6000:])),
            "trigger_evidence": AuditSanitizer.sanitize(trigger_evidence),
        }

    def _write_proposal(self, proposal: dict[str, Any]) -> None:
        self.proposals_root.mkdir(parents=True, exist_ok=True)
        path = self.proposals_root / f"{proposal['proposal_id']}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(proposal, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    async def _git(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", *arguments, cwd=str(self.source_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return ""
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return "ok"
        return stdout.decode("utf-8", errors="replace").strip()
