# Audit and CI repair closeout — 2026-09-23

Status: validated locally.

This repair set covers canonical Worker usage metadata, local read-integrity
and replay checks, CLI read safety, root-scoped session and trace exports, and
CI portability. The implemented usage contract is shared across adapters,
Candidate envelopes, admission, failure evidence, and productivity
projections. Local audit reads use consistent read-only snapshots; session
exports use a bounded revision window on a backup snapshot.

Worker adapters retain only known, valid usage fields. Strict Candidate and
harness paths reject invalid metadata without rewriting signed payloads;
sanitized receipts preserve observable closure. Raw usage metadata and usage
records preserve missing values separately from explicit zero. Productivity
token totals aggregate known counts: an empty aggregate is zero, and a total
may be derived by adding known prompt and completion counts. Neither an empty
aggregate nor a zero total proves that the provider charged nothing. Usage
reports cover only well-formed amounts explicitly returned by providers, not
a complete billing ledger.

Integrity checks validate RuntimeRecord content addresses and compare current
materialized views against a strict rebuild on an independent in-memory copy.
Replay compares manifest trace IDs with selected JSONL entries and immutable
SQLite trace records, and reports candidate decisions, effects, and accepted
outputs. Status, audit, and terminal wait checks read a consistent snapshot;
timed-out waits report integrity as unchecked. Failures expose categories and
identifiers without record payloads.

The audit result establishes local consistency only. It does not prove
runtime-log completeness, protect against a coordinated database rewrite, or
verify actor signatures against trusted Genesis and authorized keys. Those
claims require an external trusted checkpoint and signature verification.

Quick-run session manifests cover one root Contract and its descendants. Their
evidence Asset IDs come from frozen context bindings and exact IDs referenced
by tree traces; Assets committed during the run are listed separately as
produced outputs. The end revision, contracts, assets, and bounded trace reads
come from the same SQLite backup snapshot. Typed references associate
commitment traces with a root; IDs whose relation cannot be proven remain
unassigned. Input names alone do not pull every matching Asset into a session.

Target-pool cycle summaries also use the bounded RuntimeRecord window, indexed
across all record types so commitment traces can join to typed committed
effects. They report only proven Contract references, retain trace-bearing
cycles with no attributable Contract, and list unproven trace IDs separately.
Task audit's `related_runtime_traces` similarly adds only commitment-sentinel
events whose candidate and committed-record links prove a reference to the
requested Contract; entries include trace and RuntimeRecord IDs, event type,
revision, and record timestamp.

Path snapshots open source databases with SQLite `mode=ro`, reject schema
mismatches, and do not migrate or rebuild the source. Newly persisted trace
JSON uses stable key ordering; existing rows are not rewritten. The runtime
schema remains version 17 and the package release remains v0.5.11.

The shared usage conformance vectors cover absent, empty, explicit-zero,
valid, and invalid values, including numeric and multibyte-string boundaries.
CI portability fixes cover TOML serialization of Windows registry paths and
the repository-root import path needed by the AI4S example tools. Neither
change adds a live service dependency.

## Validation evidence

The final local validation passed with 1,482 tests and 3 skipped Redis
integration tests (the Redis test URL was not configured); pytest reported 6
dependency deprecation warnings. Ruff check and format check passed for 310
files across `src`, `tests`, and `scripts`. Isolated wheel and sdist builds
passed, and both distributions passed `twine check`. The wheel contained 160
Python files whose bytes matched the source tree. An installed-package smoke
test passed in a fresh temporary virtual environment and independent CLI
process. The package excludes local environment, Git, virtual-environment,
database, and private-key artifacts. The runtime schema remains 17; package
version remains v0.5.11.

Read-only diagnostics passed with unchanged records and identical before/after
digest
`cacced3a46ce9e917db5447efb2adc1a3bfb377e30e7207477b3cca26c6441a8`.
The method governance fixture completed and its rebuild matched. No live
provider was used; no release tag was created and nothing was published to
PyPI. Remote CI verification is not part of this local validation record and is
tracked separately.
Machine-readable local evidence is in
[`reports/data/068-audit-ci-repair-closeout/validation.json`](data/068-audit-ci-repair-closeout/validation.json).
