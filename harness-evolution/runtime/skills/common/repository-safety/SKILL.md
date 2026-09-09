---
name: repository-safety
description: Protect an isolated Yuan Ye Harness worktree and its Git evidence. Use before any Harness code edit, validation, candidate commit, merge, cleanup, or recovery decision.
---

# Repository Safety

1. Inspect repository status, current branch, base commit, and the assigned worktree before editing.
2. Modify only paths authorized by the Controller's current target scope, including its assigned tests and protected paths. Never modify `.git`, `.yy`, `.yy-backups`, credentials, local settings, or unrelated user work.
3. Keep all edits inside the assigned worktree. Do not use force merge, rebase, reset of the source branch, stash, or overwrite uncommitted user changes.
4. Treat validation, commit, and merge as separate facts. A verified candidate is not merged until the Engine records the durable merge result.
5. Preserve worktree, branch, commit, and audit evidence for blocked or unknown outcomes. Let the Engine perform cleanup.

Use the Gateway/Engine's durable authorization evidence for the current invocation. When valid authorization already covers the unchanged scope, continue without asking the user to confirm it again. If required evidence is absent or unverifiable, return a structured blocker to the Controller; never infer permission from the task text alone. Candidate Capability Grants, merge approvals, and Recovery decisions retain their own required approval boundaries. Approval to start an invocation does not approve a later Candidate Grant Plan.

Read relevant source and contracts within the authorized repository to resolve routine questions. Continue independent, authorized work when one step is blocked, except when UNKNOWN/recovery constraints require stopping execution. Report completed work, outstanding blockers, and validation evidence; leave final validation, merge, and cleanup decisions to the Engine.
