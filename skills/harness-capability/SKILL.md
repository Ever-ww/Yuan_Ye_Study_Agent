---
name: harness-capability
description: Safely add or repair a Yuan Ye Tool through the isolated Harness Coding Agent. Use only when Yuan Ye genuinely lacks a required Tool capability; do not use for Hook changes, ordinary workspace coding, argument mistakes, user-input mistakes, or temporary provider and network failures.
---

# Harness Capability

First attempt the existing Tools correctly. Rule out malformed arguments, schema misunderstandings, missing user input and temporary external failures.

When a real Tool gap remains, provide `harness_capability` with:

- `summary`: the missing capability.
- `desired_behavior`: observable inputs, outputs and errors.
- `current_limitation`: why existing Tools are insufficient.
- `acceptance_criteria`: concrete tests.
- `safety_constraints`: access, privacy and side-effect limits.

Call it once after high-risk approval. The target is always Tool code. If correct implementation requires dependencies, credentials, database migration, Gateway core or Tool framework changes, report that broader source review is required.

After a successful merge, end the current Run. The new Python Tool becomes available only after Gateway restart; `/skill refresh` does not hot-load it.
