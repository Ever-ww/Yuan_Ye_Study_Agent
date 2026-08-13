---
name: harness-evolve
description: Safely evolve Yuan Ye's own Tool or Hook capabilities through the isolated Harness Coding Agent. Use only when the Agent lacks a necessary Tool/Hook capability or its implementation is genuinely defective; do not use for ordinary workspace coding, tool argument mistakes, user-input mistakes, or temporary provider/network failures.
---

# Harness Capability Evolution

First decide whether the task needs a normal project-code change or whether Yuan Ye itself lacks a Tool or Hook capability. Use normal coding or `/code` for ordinary workspace development.

Before invoking `harness_evolve`, rule out malformed arguments, schema misunderstandings, missing user input, temporary external-service failures, and an existing Tool that has not been used correctly.

When a real capability gap remains, describe it precisely:

- Required user-visible behavior, inputs, outputs, errors, and security constraints.
- Why existing Tools/Hooks cannot safely provide it.
- `target="tool"` for a Tool implementation or `target="extension"` for a Hook/Extension change.

Call `harness_evolve` once with that concise task and capability gap. It is high-risk source modification and requires user approval. It runs in an isolated worktree, validates the change, and may merge only after verification.

Do not ask it to modify the Harness control path, approval rules, Tool registry/contracts, Gateway core, dependencies, credentials, database schema, or configuration. If the capability truly requires those areas, report that a broader reviewed source change is required.

After a successful result, explain that Python Tool changes require a Gateway restart. `/skill refresh` only refreshes Skill guidance; it does not hot-load Python Tools.
