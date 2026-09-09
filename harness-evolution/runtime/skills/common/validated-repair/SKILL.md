---
name: validated-repair
description: Repair a Harness candidate using concrete validation evidence. Use after a test, contract, compile, registry, or scope check fails in the current isolated invocation.
---

# Validated Repair

1. Read the latest validation summary from the current query context.
2. Identify a focused root-cause correction. Do not delete, skip, weaken assertions, or substitute weaker tests to manufacture a pass. If evidence proves a test expects an obsolete contract, correct it only within the Controller-authorized test paths and explain the contract change; otherwise report the scope blocker. Preserve coverage of the intended behavior.
3. Continue in the existing worktree and preserve already-correct changes.
4. Run the assigned focused validation before broader checks.
5. Do not claim success from model output. The Engine's authoritative validation decides whether the candidate is verified.
