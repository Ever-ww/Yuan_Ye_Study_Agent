# YYAgent test boundaries

## Current audit

The repository currently contains 50 Python test modules. The existing suite is
not replaced: filesystem, SQLite, Gateway state/recovery, backup, sandbox,
TUI, paper/reference, and harness tests remain the source of truth for those
behaviors. Their fakes are retained where they isolate a real boundary (for
example, an LLM provider, Docker, or a platform API), while the code under
test still runs through its production implementation.

The gaps found during the audit were:

- no deterministic local test covering Gateway -> Runtime -> Tool -> durable
  Event -> Session JSONL as one chain, including streaming;
- no regression test for a partially materialized derived runtime profile;
- no filesystem-watcher assertion that an external edit refreshes the Note
  index and callback;
- no explicit separation for tests that need a real third-party provider;
- a TUI submission repaint race could hide the prompt before provider output.

Those gaps are covered by `test_local_agent_integration.py`, the regression
tests in `test_runtime_resources.py`, `test_notes.py`, and
`test_chat_tui.py`, plus `test_external_services.py`. The local integration
uses a loopback HTTP provider only at the external provider boundary; it does
not fake Gateway, Runtime, Tool dispatch, persistence, or event handling.
It now exercises both complete and SSE provider responses, reconstructs the
Gateway against the same durable stores, and verifies that restoring one
Workspace leaves a second Workspace unchanged.

The current frontend test inventory is intentionally smaller: the existing
Vitest API/event tests are retained and the production build is checked. A
browser-level Playwright suite is still a separate follow-up and is not
claimed by the Python integration test.

The default suite is deterministic and may use temporary files, SQLite, a
loopback provider server, and the real Gateway and Runtime layers. A local
provider boundary is kept at the HTTP edge; the request, tool dispatch, state
transitions, event store, session JSONL, and workspace remain production code.

The important default commands are:

```text
uv run pytest -m "not external"
uv run python -m unittest discover -s tests -p "test_*.py"
uv run python -m compileall Agent backup bootstrap context_process extension gateway harness_runtime memory prompt sandbox skill tool tools run_ui tests harness-evolution run.py
```

`integration` tests are local end-to-end checks and run in the normal suite.
`native` tests require the platform sandbox and skip only when that facility is
not available. Tests needing a real provider key or a public service must be
marked `external` and run separately, for example:

```text
YY_RUN_EXTERNAL_TESTS=1 uv run pytest -m external
```

On PowerShell, set the same opt-in explicitly before running the command:

```powershell
$env:YY_RUN_EXTERNAL_TESTS = "1"
uv run pytest -m external
```

An external test must report a skipped or not-run result when its required
credential or endpoint is absent. A passing local provider test only proves the
client protocol and the local production chain; it never claims that a public
LLM account or third-party service was verified.

Browser E2E lives in `e2e/` and intentionally uses a separate package so the
normal frontend unit-test install does not require a browser download. Start
`uv run python run.py serve-ui`, put its one-time URL in `YY_E2E_URL`, then run
`npm install`, `npm run install:browsers`, and `npm test` inside `e2e/`.
The suite reuses the authenticated browser storage state and checks the real
Agent/Read/Write/Note/Code route shell plus keyboard, focus, tooltip,
reduced-motion, sidebar, and narrow-viewport behavior.

When adding a test, name it after the behavior it proves. The assertion should
check the observable side effect where possible: files and hashes, SQLite
rows, durable events, session records, workspace state, or restored output.
Mock only a system boundary such as a provider or public service. Keep the
Gateway, Runtime, Tool dispatcher, Sandbox policy, persistence, and recovery
logic real for tests that claim those layers.
