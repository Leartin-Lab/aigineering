# Runtime stability closeout — 2026-09-22

Commit `be7aec0` closes the approved S1/S2 runtime stability scope on macOS
with Python 3.11.15 and SQLite 3.53.4. The runtime remains schema version 17;
this work adds no data or schema migration.

S1 preserves well-formed provider usage when response extraction, tool mapping,
or Candidate-envelope compilation fails. Failure metadata uses a small explicit
allowlist with bounded UTF-8 identity strings and safe non-negative token
counts; unknown fields and invalid values are dropped, and absent usage is not
invented.
The failure remains visible through trace, task status, and audit views. See
[change 026](../changes/026-worker-failure-usage-evidence.md) and the focused
coverage in `tests/test_llm_worker.py` and `tests/test_worker_routing.py`.

S2 adds `get_by_relation_target` to the trace/store capabilities and all built-in
adapters. Continuation method-context lookup now uses the targeted query while
preserving event filtering, row order, de-duplication, missing-asset skipping,
and disclosure redaction. SQLite initializes the ordinary relation-target index
idempotently, including when reopening an existing database. Custom
`OperationalStore` adapters must implement the new method. See
[change 027](../changes/027-targeted-continuation-trace-query.md) and coverage
in `tests/test_store_conformance.py` and `tests/test_worker_commands.py`.

Validation completed:

- `pytest -q`: **1439 passed, 3 skipped, 6 warnings** in 52.83s.
- `ruff check` and `ruff format --check`: passed for 300 files.
- Isolated `python -m build`: wheel and sdist succeeded; `twine check` passed.
- Wheel and sdist excluded `.internal-docs`, `.env`, `.git`, `.venv`, database
  files, and private keys; wheel Python sources matched the source tree byte for
  byte.
- Installed smoke validation passed, including `fixture` method governance
  `complete` and `rebuild_match=true`.
- Diagnostics reported `records_unchanged=true` and matching digest
  `db83054ceb16c1243e9fbc018f2ed05eed7fe5ef944467ee5ee39521dc6916c6`.
- Machine-readable summary: [validation.json](data/067-runtime-stability-closeout/validation.json).

Artifact hashes:

- wheel: `1da528622b8c7d480cde0db202c8ee79b52abf6a701fa422860921b660d48547`
- sdist: `46efc860f307966f47551cb4e40248bbc63f63d727dd13ccf1235a5f17277b48`

This validates the implementation for a direct main-branch update. It does not
publish a tag or PyPI release. No live provider credentials were available, so
20+ chain and nested-replan scenarios were not exercised in this run. Unknown
usage fields remain unknown; the runtime does not claim to provide a complete
cost ledger.
