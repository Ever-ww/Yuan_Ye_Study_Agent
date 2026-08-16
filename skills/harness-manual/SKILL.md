---
name: harness-manual
description: Guide a user into Yuan Ye's persistent `/code` Harness mode for Hook behavior changes. Use when the requested change belongs under extension/hook rather than a missing Tool, a RuntimeFailure repair, or ordinary workspace development.
---

# Harness Manual

Confirm that the requested change is a Yuan Ye Hook behavior change. Use ordinary coding for user workspace code and `harness-capability` for a missing Tool.

Ask the user to enter `/code`. Keep the resulting Code Session active across multiple turns until the user finalizes or aborts it. Do not call an internal MANUAL Tool from the model; Gateway owns the `/code` lifecycle, worktree, validation and merge.

Limit writes to `extension/hook/**` and `tests/extensions/**`. Preserve the user's original Session as read-only origin context.
