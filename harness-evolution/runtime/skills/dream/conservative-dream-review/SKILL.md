---
name: conservative-dream-review
description: Conservatively review and improve the exact files in an authorized Harness DREAM changeset. Use only for behavior-preserving maintenance after verified Harness merges.
---

# Conservative Dream Review

1. Read relevant callers, implementations, and contracts throughout the authorized source repository to assess the changeset. Write only the Controller-authorized changeset files and test paths, subject to all protected paths; read access does not expand write scope.
2. Make changes only when they remove a concrete defect, duplication, fragile error handling, or missing test coverage.
3. Preserve public APIs, Tool schemas, risk, idempotency, permissions, configuration, dependencies, and database schemas.
4. Do not modify Harness approval, merge, reconcile, credential, or Backup/Restore controls.
5. Make no change when no safe improvement is justified.
