# ADR-021: Reproducible release evidence

Status: Accepted
Date: 2026-09-06
Related: ADR-010, ADR-011, ADR-020

## Context

The v0.5.6 live report retains an unexplained reconstruction digest mismatch.
Without the pre-rebuild database, non-reproduction cannot explain the original
failure. Manual release checks also leave optional integrations, installed
artifacts, and supported Python versions outside a repeatable gate. Full-history
projection reads need measurements before an optimization changes their inputs.

## Decision

Reconstruction diagnostics open the source read-only and use SQLite backup to
capture committed WAL data. They retain that snapshot and rebuild a second
private copy. A machine-readable manifest records semantic digests, canonical
record preservation, table row counts/fingerprints, and an explicit passing,
mismatching, or error outcome. An existing evidence directory is never reused.
Current-schema verification does not silently perform historical migrations.
The source is never repaired by the diagnostic adapter.

The databases remain private local artifacts; the manifest does not export row
contents or exception messages. CI may retain only its generated fixture stores.
Diagnostics are an application adapter and cannot enter the commitment kernel.

CI checks Python 3.11–3.13 with API and Redis dependencies and a real Redis
service, then checks an installed wheel outside the source tree. Publication
reuses that validated artifact. Bounded deterministic local benchmarks report
workload, environment, revision, timings, and memory without a hardware-specific
performance pass threshold. AST dependency guards supplement behavioral and
existing source-text architecture tests.

## Consequences

- future reconstruction failures retain the evidence needed for diagnosis;
- fixture-backed installation and integration checks can be repeated;
- measurement precedes projection-context optimization;
- retained snapshots contain full database data and require private handling;
- passing evidence does not explain the historical v0.5.6 mismatch, prove
  scientific truth, or add sandboxing, MCP transport, or exactly-once effects.

## Verification

- `tests/test_reconstruction_diagnostics.py`
- `tests/architecture/test_dependency_boundaries.py`
- `tests/test_benchmark_runtime.py`
- `.github/workflows/ci.yml`
- `scripts/installed_smoke.py`
