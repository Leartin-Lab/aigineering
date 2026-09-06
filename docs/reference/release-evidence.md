# Release evidence and reconstruction diagnosis

Use the repository version and a clean commit when publishing acceptance
results. A local implementation commit is not a GitHub release or PyPI upload.
CI runs deterministic tests on Python 3.11, 3.12, and 3.13 with API dependencies
and a Redis 7 service. It builds and checks distributions and installs a wheel
in a fresh environment outside the repository. Publication reuses that wheel
and sdist after the complete reusable CI job succeeds.

## Diagnose reconstruction without changing the source

```bash
python -m aigineering.diagnostics .aig/store.db \
  --output-dir /private/tmp/aig-rebuild-evidence
```

The output directory must not exist; its parent must exist. On systems without
`/private/tmp`, choose a private temporary directory on that system.
The tool creates `before.sqlite`, `rebuilt.sqlite`, and `manifest.json`. SQLite
backup captures committed WAL contents consistently. Only the second copy is
rebuilt. The manifest compares the runtime semantic digest and canonical-record
table fingerprint and lists table-level differences without raw row contents.
Physical table differences are diagnostic clues; semantic digest and unchanged
canonical records determine success. Older schema versions are retained but
rejected for verification; use their matching runtime or perform a separately
reviewed migration first.

Exit codes are 0 for a passing comparison, 1 for a mismatch or recorded
verification error, and 2 for invocation/setup errors. Preserve the full output
directory after a failure. The snapshots contain original data, including any
sealed values; keep them private and do not upload real user databases as CI
artifacts. Files use owner-only permissions on POSIX. The manifest reports
error categories rather than exception text to avoid exposing database content.

The live source can continue to change after capture; results describe the
captured snapshot, not later writes. This command neither repairs the source
nor claims to explain the diagnostic mismatch retained in the v0.5.6 report.

## Measure growth before changing the kernel

For a bounded deterministic baseline:

```bash
python scripts/benchmark_runtime.py --sizes 10 100 300 --samples 3 \
  --output /private/tmp/aig-runtime-benchmark.json
```

The output path must not exist. Run `python scripts/benchmark_runtime.py --help`
for options. Record the exact command, package/commit identity, dirty-tree status,
Python/SQLite versions, history sizes, sample count, actual record counts,
latencies, and traced Python memory. Use repeated runs on the same hardware to
compare changes. Traced memory is not process RSS, and mock/synthetic throughput
is not model productivity or scientific quality.

The current projection context reads the full fact history. The benchmark
establishes a baseline; v0.5.8 does not claim to remove that scaling cost.

## Remaining release evidence

Real provider scenarios, external security review, process-level tool isolation,
and semantic-quality evaluation are separate evidence obligations. Passing
fixture CI does not establish them. CI configuration changes also do not count
as remote execution evidence until the workflow actually runs.
