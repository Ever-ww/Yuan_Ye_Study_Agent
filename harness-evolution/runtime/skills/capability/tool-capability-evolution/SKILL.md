---
name: tool-capability-evolution
description: Add or repair an approved Yuan Ye Tool capability from a structured CapabilityGap. Use only in a CAPABILITY Harness trace restricted to Tool implementation, Tool tests, and minimal registration changes.
---

# Tool Capability Evolution

1. Read the CapabilityGap, acceptance criteria, safety constraints, and existing Tool contracts.
2. Prefer extending an existing Tool when that preserves a clear contract; otherwise create one uniquely named Tool.
3. Write only `tools/**`, `tests/tools/**`, and the explicitly permitted registration files.
4. Validate JSON Schema, risk, idempotency, runtime profile, registration, and unrelated Tool contract stability.
5. Stop with `requires_broader_source_change` if correct implementation needs dependencies, credentials, services, schema migrations, or Tool framework changes.
