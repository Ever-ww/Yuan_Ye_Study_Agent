---
name: harness-evolve
description: Compatibility guidance for the former `harness_evolve` capability entry. Use only when resuming an older Session or durable operation that still references the old name; new work must use the `harness-capability` Skill and `harness_capability` Tool.
---

# Harness Capability Compatibility

For new capability gaps, read `skills/harness-capability/SKILL.md` and call `harness_capability`.

Use the old `harness_evolve` name only to resume an already-open legacy Session or reconcile an existing durable operation. It delegates to the same CAPABILITY service and must not create a second Harness implementation.

The remaining rules are retained for old Session snapshots:

First decide whether the task is ordinary workspace development, a `/code` Hook change, or whether Yuan Ye itself lacks a Tool capability. Use normal coding for ordinary workspace development and `/code` for Hook behaviour.

Before invoking `harness_evolve`, rule out malformed arguments, schema misunderstandings, missing user input, temporary external-service failures, and an existing Tool that has not been used correctly.

When a real capability gap remains, describe it precisely:

- `summary`: the concise missing capability.
- `desired_behavior`: observable inputs, outputs and errors.
- `current_limitation`: why existing Tools cannot safely complete it.
- `acceptance_criteria`: concrete, testable requirements.
- `safety_constraints`: access, privacy and side-effect limits.

Call `harness_evolve` once with the task and structured `capability_gap`. The target is always Tool code; do not pass a target selector. It is high-risk source modification and requires user approval. It runs in an isolated worktree, validates the change, and may merge only after verification.

Do not ask it to modify the Harness control path, approval rules, Tool registry/contracts, Gateway core, dependencies, credentials, database schema, or configuration. If the capability truly requires those areas, report that a broader reviewed source change is required.

After a successful result, explain that Python Tool changes require a Gateway restart. `/skill refresh` only refreshes Skill guidance; it does not hot-load Python Tools.
