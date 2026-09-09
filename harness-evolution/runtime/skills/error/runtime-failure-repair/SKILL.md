---
name: runtime-failure-repair
description: Repair a confirmed Yuan Ye source defect from durable RuntimeFailure evidence. Use only in an ERROR Harness trace with a real ErrorSnapshot and approved source-repair scope.
---

# Runtime Failure Repair

1. Analyze the referenced ErrorSnapshot, including its actual messages, tools, model identity, traceback, and retry history. Prefer a local minimal reproduction or regression test; fixtures/mocks may isolate external services while preserving the failure conditions. Do not replay side-effecting production calls solely to reproduce the error. If exact reproduction is unavailable, continue evidence-supported diagnosis and repair, disclose the validation gap, and let the Engine enforce verification requirements.
2. Distinguish a source defect from network, provider, configuration, permission, malformed arguments, or user-input failures.
3. Make the smallest source and test changes that correct the demonstrated defect.
4. Preserve durable runtime, recovery, approval, and credential boundaries.
5. Use each failed validation as repair evidence. Apply the common validated-repair rules for test corrections; never hide failures or weaken coverage to manufacture success.
