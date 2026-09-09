---
name: hook-evolution
description: Implement user-requested Yuan Ye Hook behavior in a MANUAL /code invocation. Use for edits restricted to extension/hook, controller-assigned extension tests, and explicitly permitted Extension documentation.
---

# Hook Evolution

1. Read `extension/README.md` and the relevant Hook implementation before editing.
2. Write only under `extension/hook/**`, the controller-assigned paths in `tests/extensions/**`, and `extension/README.md` when required by the task and explicitly allowed by the Controller. Directory-level guidance never expands the assigned test paths.
3. Create or update only the controller-assigned test path from the current query context.
4. Preserve Hook names, signatures, ordering, and lifecycle semantics unless the request explicitly requires a compatible change.
5. Validate the focused test and let the Engine run the complete pipeline.
