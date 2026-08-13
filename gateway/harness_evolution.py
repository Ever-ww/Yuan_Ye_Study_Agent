"""Gateway bridge for the CAPABILITY Harness trigger.

It deliberately calls the Harness engine directly instead of creating a nested /code workload.
"""

from __future__ import annotations

import importlib.util
import json
import asyncio
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from Agent import RuntimeConfig


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
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.source_root = (config.coding_source_root or Path(__file__).resolve().parents[1]).resolve()
        self.module = _load_harness(self.source_root)

    async def evolve_capability(
        self, *, operation_id: str, task: str, target: str, capability_gap: str,
    ) -> dict[str, Any]:
        request = self.module.HarnessEvolutionRequest(
            task=task,
            config=self.config,
            trigger="capability",
            target=target,
            source_root=self.source_root,
            agent_root=self.config.agent_root,
            operation_id=operation_id,
            capability_gap=capability_gap,
            max_attempts=4,
            merge_policy="immediate",
        )
        result = await self.module.HarnessEvolutionEngine.for_config(self.config).run(request)
        return result.model_dump(mode="json")

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
            # The durable invocation allows Recovery to inspect this exact branch/commit pair;
            # do not speculate or re-run a non-idempotent evolution here.
            return {"status": "UNKNOWN", "evidence": {"invocation_id": invocation_id, "merge_intent": intent}}
        return {"status": "UNKNOWN", "evidence": {"invocation_id": invocation_id, "reason": "incomplete invocation audit"}}

    async def _git(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", *arguments, cwd=str(self.source_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        return stdout.decode("utf-8", errors="replace").strip() if process.returncode == 0 else ""
