# v0.5.7 tool validation patch evidence

Date: 2026-09-06
Baseline implementation: `c8c2525` (v0.5.6).
Candidate: v0.5.7 source tree on `dev`; local commit preparation, not publication.
Environment: Darwin arm64, Python 3.11.15.

## Scope

This patch rejects non-object tool arguments before handler execution,
distinguishes booleans from numbers in recursive schema enum/const comparison,
and rejects explicit null schema keywords except `const`. Historical ADR
implementation notes now point to the current Plugin boundary. Protocol
identity, SQLite schema, and commitment ownership remain unchanged.

## Verification

Targeted ToolWorker tests passed (12 tests), including non-object arguments
with an unconstrained schema and zero handler invocations. Targeted schema
checks passed (28 tests), including nested JSON equality and registration
failure without replacing a working handler.

Closing commands and observations:

- `ruff check src/aigineering tests` and `ruff format --check src/aigineering tests`
  passed (235 Python files).
- `pytest -q`: 1,242 passed, 3 Redis integration skips, one existing
  Starlette/httpx warning, 52.70 seconds.
- `tests/test_tool_argument_rejection.py` additionally exercises signed
  publication, WorkerHost, SQLite close/reopen, and digest-preserving rebuild
  of the typed error observation without handler execution.
- `python -m build` passed with isolated build environments; `twine check`
  accepted wheel and sdist.
- The 0.5.7 wheel installed into the temporary dependency environment used for
  0.5.6 acceptance; separate CLI processes passed signed domain creation, task
  creation, explicit mock execution, completed status, and productivity audit.
- `git diff --check` passed.

The initial full gate caught the old hard-coded DESIGN version assertion and
an unfinished report link. Both were corrected before the passing closing run;
the version guard now checks the package version rather than a fixed release.

## Limits

The v0.5.6 diagnostic rebuild mismatch remains unexplained. This patch does
not add process isolation, timeout enforcement, external-effect idempotency,
or production MCP. No new live LLM or real Redis evidence is claimed.
