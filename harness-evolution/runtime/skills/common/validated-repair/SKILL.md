---
name: validated-repair
description: Repair a Harness candidate using concrete validation evidence. Use after a test, contract, compile, registry, or scope check fails in the current isolated invocation.
---

# Validated Repair

1. Read the latest validation summary from the current query context.
2. Identify the smallest root-cause correction; do not restart the implementation or create substitute tests.
3. Continue in the existing worktree and preserve already-correct changes.
4. Run the assigned focused validation before broader checks.
5. Do not claim success from model output. The Engine's authoritative validation decides whether the candidate is verified.
