---
name: runtime-failure-repair
description: Repair a confirmed Yuan Ye source defect from durable RuntimeFailure evidence. Use only in an ERROR Harness trace with a real ErrorSnapshot and approved source-repair scope.
---

# Runtime Failure Repair

1. Read the referenced ErrorSnapshot and reproduce the failure from its actual messages, tools, model identity, traceback, and retry history.
2. Distinguish a source defect from network, provider, configuration, permission, malformed arguments, or user-input failures.
3. Make the smallest source and test changes that correct the demonstrated defect.
4. Preserve durable runtime, recovery, approval, and credential boundaries.
5. Use each failed validation as repair evidence; never hide or replace the failing test.
