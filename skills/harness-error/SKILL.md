---
name: harness-error
description: Explain and supervise Yuan Ye's RuntimeFailure-driven Harness repair flow. Use only after Gateway has captured a real snapshot-worthy RuntimeFailure and produced an Error Evolution proposal; never fabricate an error snapshot or use this for network, provider, permission, input, or argument failures.
---

# Harness Error

Require a real Gateway Error Evolution proposal and its persisted RuntimeFailure snapshot. Preserve messages, Tool schemas, model identity and retry history from `runtime.last_failure`.

Let the user make the durable high-risk decision once. After approval, Gateway invokes the internal ERROR adapter and the shared Harness Engine. Do not expose or call `harness_error` as a model Tool.

Treat deterministic validation failure as failed evidence. Treat uncertain merge or source effects as UNKNOWN and preserve the worktree, branch and audit for recovery.
