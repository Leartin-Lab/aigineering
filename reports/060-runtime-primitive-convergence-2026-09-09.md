# v0.5.10 runtime primitive convergence evidence

Date: 2026-09-09
Baseline: `af0404f` with the documented v0.5.10 working-tree changes.
Scope: local development verification; no remote publication or production
deployment claim.
Environment: macOS arm64, project Python 3.11.15, SQLite 3.53.4.

## Verified closure

The v0.5.10 compatibility slice converges repeated Contract construction and
runtime maintenance, narrows Store capabilities, separates pure SQLite row
materialization, canonicalizes trace-to-record conversion, completes HTTP
Contract views, and moves identity/signing ownership to the protocol layer while
retaining object-identical compatibility exports from the former core paths.

Administrative recovery retains the source Contract's acceptance policy.
Completion Plugins no longer call the Store commitment primitive; the core
lifecycle owner commits derived terminal and trace records in one authoritative
transaction. External trace sinks remain post-commit exports and cannot redefine
accepted facts.

## Compatibility evidence

- Package version reports `0.5.10`.
- Existing v3, v4 and v5 Contract identity behavior remains covered by the
  conformance and identity suites.
- `aigineering.core.ids` and `aigineering.core.signing` re-export the same Python
  objects as their canonical protocol modules.
- Protocol modules have no reverse import from `aigineering.core`, including
  type-only imports.
- The Candidate and RuntimeRecord wire formats and SQLite schema are unchanged by
  this compatibility slice.
- SQLite reopen, reconstruction, claim fencing, terminal single assignment,
  disclosure, acceptance, planning, recovery and local Fleet composition remain
  covered by the full deterministic suite.

## Commands and results

```text
.venv/bin/ruff check src/aigineering tests
All checks passed!

.venv/bin/ruff format --check src/aigineering tests
269 files already formatted

.venv/bin/pytest -q tests/architecture
216 passed

.venv/bin/pytest -q
1307 passed, 3 skipped, 6 warnings in 54.00s

.venv/bin/python -m build --no-isolation
Successfully built aigineering-0.5.10.tar.gz and
aigineering-0.5.10-py3-none-any.whl

.venv/bin/twine check dist/aigineering-0.5.10*
wheel: PASSED
sdist: PASSED

git diff --check
passed
```

The skipped tests require optional external services or dependencies. The six
warnings are dependency deprecations from Starlette/httpx and PyMuPDF SWIG
types; they are not runtime assertion failures.

During parallel review, one concurrent full-suite process observed an invalid
local Ed25519 key fixture while another process was using the shared pytest
temporary-root hierarchy. The affected Local Fleet test passed five consecutive
isolated runs, and the subsequent serialized full suite passed. This is retained
as test-environment evidence, not classified as a signing or Fleet regression.

## Explicit limits

No fresh real-provider, Redis, production MCP, hostile-network, cross-machine,
external side-effect exactly-once, or external security validation is claimed.
This report proves the local compatibility and deterministic release gates only.

Physical removal of the public `core.store.JsonLStore` owner, incremental
consequence watermarks, deeper SQLite ledger extraction, Contract identity v6,
and process-level tool isolation remain future slices.

## CI convergence follow-up

The v0.5.8 and v0.5.9 Ubuntu CI failures were inspected before promotion.
Recovery rebuild differed because live trace insertion retained the first
observation timestamp while reconstruction selected the last timestamp for the
same semantic trace ID. A synchronized two-connection regression fails against
the previous rebuild method and passes with the first-observation replay rule.
The full trace values, not just the semantic digest, now agree after rebuild.

The Fleet key provisioning race is fixed with complete-file, atomic no-replace
publication. Deterministic thread contention and spawned-process publisher tests
exercise public entry points using independent SQLite connections. Existing key
bytes and private permissions remain unchanged, with no temporary-file residue.
See `changes/020-local-concurrency-reconstruction.md`.

A local full suite during this follow-up passed 1,309 tests with 3 optional skips
in 53.90 seconds. After the final key-publication refinement, all 7 identity/Fleet
tests passed. Ruff lint and format passed over source, tests and scripts (272
files). Isolated build and Twine passed; fresh installed-wheel smoke, separate
CLI processes and backup-first reconstruction passed. Final remote CI remains
the promotion gate; no remote success is inferred from these local observations.

The v0.5.6 diagnostic had no retained pre-rebuild snapshot. The identified CI
cause does not retroactively prove the cause of that historical mismatch.

## Final promotion follow-up

A later Ubuntu Python 3.12 run exposed a second Fleet race between continuation
declaration and scheduling-context publication. Exact observation context now
commits before the continuation Candidate becomes claimable. The preparation
record does not declare work or indicate successful scheduling. The final
scheduling trace remains compatible with prior histories; ordinary and parallel
continuations retain their existing input semantics.

The installed-wheel smoke now selects Windows virtualenv executable paths rather
than assuming POSIX `bin/`. The platform scan confirmed conditional POSIX mode-bit
assertions and capability-based symlink skips. Domain and reconstruction tests
passed (13 tests); this was a source-level portability review on macOS, not a
Windows runner validation. Current CI platforms are documented in CONTRIBUTING.
