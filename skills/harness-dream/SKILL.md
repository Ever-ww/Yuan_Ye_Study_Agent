---
name: harness-dream
description: Operate Yuan Ye's opt-in nightly Harness code review. Use for Harness Dream status, freeze, explicit daily runs, blocked-change approval, or revert candidates; the model must not invoke the internal `harness_dream` adapter directly.
---

# Harness Dream

Keep `harness_dream_enabled` disabled unless the user explicitly accepts unattended verified fast-forward merges and safe idle restart.

Automatic Dream reviews at most one immutable daily changeset containing MANUAL, ERROR and CAPABILITY merges visible at the tick cutoff. It ignores ordinary commits and DREAM-generated commits. If no eligible code changed, it creates no Harness Run, Operation, worktree or Coding Runtime.

Use Gateway commands for `status`, `run`, `freeze`, `unfreeze` and `revert`. Do not call the internal `harness_dream` adapter as a model Tool. A revert first creates an isolated candidate and requires a separate high-risk approval before merge.

Never automatically retry DEFERRED, BLOCKED, FAILED or UNKNOWN results. Preserve UNKNOWN evidence for reconcile or human recovery.
