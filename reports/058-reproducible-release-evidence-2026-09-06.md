# v0.5.8 reproducible release evidence

Date: 2026-09-06
Baseline: `4431c85` (v0.5.7).
Candidate: v0.5.8 local source tree on `dev`; no publication is claimed.
Environment: Darwin arm64, Python 3.11.15, SQLite 3.53.0, Twine 7.0.0.

## Scope

Backup-first reconstruction diagnosis, signed-publication scaling measurements,
AST dependency guards, Python/API/Redis CI coverage, and installed-artifact
verification. The source Store and commitment protocol retain their existing
ownership and schema.

## Reconstruction evidence

Six behavior tests cover consistent committed WAL capture, source semantic
preservation, retained mismatching materializations, error category recording
without raw data, refusal to overwrite existing evidence, nonzero CLI outcomes,
and refusal to silently migrate historical schemas. Deliberate SQL corruption
in the tests is fault injection after signed publication, not a fact admission
path. A separate read-only implementation review found no diagnostic blocker.
Read-only SQLite may create WAL/shared-memory sidecars; the source facts and
main database contents are not rebuilt or repaired.

## Closing validation

- `ruff check src/aigineering tests scripts` and
  `ruff format --check src/aigineering tests scripts` passed (241 files).
- `pytest -q` without Redis: 1,255 passed, 3 skipped, 54.41 seconds.
- `AIG_REDIS_TEST_URL=redis://127.0.0.1:32768/15 pytest -q`:
  **1,258 passed, no skips**, 53.24 seconds. A dedicated ephemeral local
  `redis:alpine` container reported Redis 8.4.0; it was removed after testing.
  CI config uses Redis 7, whose remote execution is not claimed here.
- Both runs retained the existing Starlette/httpx deprecation warning.
- `python -m build` passed in isolated environments; Twine accepted wheel
  and sdist. A final no-isolation packaging pass includes the closing evidence.
- `python scripts/installed_smoke.py --wheel
  /private/tmp/aigineering-058-release/aigineering-0.5.8-py3-none-any.whl
  --expected-version 0.5.8 --source-root /Users/gaoyan/projects/aigineering
  --evidence-dir /private/tmp/aigineering-058-reconstruction` passed.
  The script created a fresh dependency environment and verified imports outside
  the repository, signed publication, explicit mock execution, separate-process
  status/audit, backup-first reconstruction, and source reopen.
- The installed diagnostic reported matching semantic digests
  `c947f9d334ec101fe33e3b51ea2857993b975251c0fdf0071269318969156b20`
  and unchanged canonical records. Table fingerprints identified only
  `created_at`/`updated_at` differences in claim and registration materializations;
  these timestamps are outside the semantic digest. Raw fixture databases remain
  in the private temporary evidence directory, not the repository.
- The final locally built wheel has SHA-256
  `67308162af4502c7c6639989fca00712aed040459a3ff37aa2e1c43abc581819`.
  Its runtime files match the isolated-build wheel; build-tool-generated
  distribution metadata differs, so the final wheel also passed its own
  fresh installed smoke check using `--wheel /private/tmp/aigineering-058-final/aigineering-0.5.8-py3-none-any.whl`
  and `--evidence-dir /private/tmp/aigineering-058-final-reconstruction`. The sdist includes both scripts, ADR-021, this
  report, and the raw benchmark JSON.
- Both workflow files parsed successfully and `git diff --check` passed.
  No GitHub Actions run, Python 3.12/3.13 local execution, or PyPI publication
  is claimed by these observations.

## Reproducible scaling baseline

Command:

```bash
python scripts/benchmark_runtime.py --sizes 10 100 300 --samples 3 \
  --output /private/tmp/aigineering-058-benchmark.json
```

The [raw measurements](data/058-runtime-benchmark.json) retain per-sample
values, environment, actual counts, and the dirty candidate tree based on
`4431c85d61a9443beb342c58370bb29f0e5d0d03`. Each sample builds history through
signed asset/Contract publication in a fresh SQLite domain, measures one more
publication, audits one selected task, and verifies reconstruction against the
post-commit/pre-rebuild semantic digest.

| History items | Records after measured commit | Commit median ms | Audit median ms | Rebuild median ms | Commit peak traced bytes |
| --- | --- | --- | --- | --- | --- |
| 10 | 89 | 13.55 | 2.71 | 14.96 | 359,583 |
| 100 | 809 | 102.48 | 25.43 | 138.31 | 3,414,422 |
| 300 | 2,409 | 302.43 | 84.03 | 447.61 | 10,332,638 |

All nine reconstruction comparisons passed. Timings include tracemalloc
overhead and were collected on a shared machine while validation was running.
With only three samples, interpolated p95 is descriptive, not a reliable tail
latency estimate. These values demonstrate the workload's growth cost and
motivate scoped context loading; they do not establish a production capacity
limit or an optimized runtime.

## Unresolved limitations

The v0.5.6 diagnostic reconstruction mismatch remains unexplained. Retaining
new evidence is a prevention of evidence loss, not a root-cause fix. Full-history
context loading remains unchanged; the benchmark is a synthetic local baseline,
not a semantic-quality or provider-cost claim. Process isolation, production
MCP, and exactly-once external effects remain future work. Real user database
snapshots are private and must not be included in public release artifacts.
