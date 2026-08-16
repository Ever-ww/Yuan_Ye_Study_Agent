---
name: hook-evolution
description: Implement user-requested Yuan Ye Hook behavior in a MANUAL /code invocation. Use for edits restricted to extension/hook and the controller-assigned extension tests.
---

# Hook Evolution

1. Read `extension/README.md` and the relevant Hook implementation before editing.
2. Write only under `extension/hook/**` and `tests/extensions/**`.
3. Create or update only the controller-assigned test path from the current query context.
4. Preserve Hook names, signatures, ordering, and lifecycle semantics unless the request explicitly requires a compatible change.
5. Validate the focused test and let the Engine run the complete pipeline.
