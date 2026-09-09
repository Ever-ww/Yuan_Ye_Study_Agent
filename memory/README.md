# Durable long-term memory

`memory.sqlite3` is the only authority for long-term memory facts, evidence,
mutation history, and projection intent. Conversation JSONL remains the
short-term transcript. `index.sqlite3` and files under `profile/` are
rebuildable projections and are never consulted as business truth.

Canonical writes commit a record/evidence/mutation plus pending index and
profile jobs in one `memory.sqlite3` transaction. Index and profile workers run
after that commit. They use stable job identities and idempotent UPSERT/replace
operations; there is deliberately no transaction spanning two SQLite databases
or SQLite and the filesystem.

Runtime recall is Hook driven. Scope access and a store watermark are frozen at
`TURN_START`; every model request in that Turn sees the same
`MemoryTurnSnapshot`. Facts committed after `TURN_START` become visible next
Turn. The latest Session continuation summary is mandatory on every model
request, as a separate ephemeral fragment without rerunning retrieval. New
compression includes every prior summary from that Session. Historical summary
recall is separately opt-in via `memory_recall_summaries` (default `false`).
The bound `session_history` tool can read original conversation segments using
the source references recorded with each summary. See
[summary continuity](../docs/summary-continuity.md) for limits and usage.

Memory fragments are provider-only. They are stripped or rejected by the
Session persistence boundary and must never enter transcripts, summaries,
Inbox records, failure snapshots, tool arguments, or long-term memory.

Recall is an enhancement: index/embedding/audit failures degrade to a scoped,
bounded canonical lookup or empty recall. Canonical fact writes remain
fail-closed. Security and permission rules must therefore remain in stable
prompts and runtime policy, never solely in recalled memory.
