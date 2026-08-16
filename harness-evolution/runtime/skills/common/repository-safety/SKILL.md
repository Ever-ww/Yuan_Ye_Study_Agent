---
name: repository-safety
description: Protect an isolated Yuan Ye Harness worktree and its Git evidence. Use before any Harness code edit, validation, candidate commit, merge, cleanup, or recovery decision.
---

# Repository Safety

1. Inspect repository status, current branch, base commit, and the assigned worktree before editing.
2. Modify only paths authorized by the current Harness trigger. Never modify `.git`, `.yy`, `.yy-backups`, credentials, local settings, or unrelated user work.
3. Keep all edits inside the assigned worktree. Do not use force merge, rebase, reset of the source branch, stash, or overwrite uncommitted user changes.
4. Treat validation, commit, and merge as separate facts. A verified candidate is not merged until the Engine records the durable merge result.
5. Preserve worktree, branch, commit, and audit evidence for blocked or unknown outcomes. Let the Engine perform cleanup.
