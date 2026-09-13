# Runtime debt closeout — 2026-09-13

The v0.5.11 development tree based on `f99d8a7` closes concrete defects found by
configuration review and the native guarded-review composition.

| Boundary | Correction | Evidence |
| --- | --- | --- |
| Planning acceptance | Compile retains complete parent policy; required verification remains reachable | Planning acceptance architecture regression and report 065 |
| Attestation wire adapter | Frozen array tuples are accepted as the internal form of JSON arrays | Signed SQLite acceptance with nonempty evidence |
| Independent completion | Child cancellation shares the ordinary reducer in the qualification transaction | SQLite cancellation/rebuild regression and final live closure |
| Fleet and LLM numbers | Strict TOML types, shared finite numeric validation, non-negative integer retry count, fractional timeout preserved | Fleet/LLM tests cover bool, strings, fractions, NaN/Inf and bounds |
| HTTP provider response | Read at most 4 MiB + 1; fail closed on oversize, bad UTF-8 and excessively nested JSON | Transport read-bound and no-retry tests |

See [change 024](../changes/024-guarded-review-closure.md),
[change 025](../changes/025-provider-configuration-bounds.md), and
[all live evidence](065-guarded-claim-review-2026-09-13.md). No schema migration,
new domain-specific kernel effect, or alternative durable write path was added.

Ruff lint and format checks passed. The full suite passed with **1428 passed,
3 skipped**. Eight live-store reconstructions preserved materialization digests
and durable records. The final live root and all descendants have terminal facts. Isolated wheel/sdist
build passed; artifact inspection found no private workspace, credentials or
SQLite files. Public reference and changed-file link checks passed.

Remaining scope is explicit: failed model compilation can omit usage metadata,
so report 065 publishes a subtotal; method-context lookup still merits a separate
indexing benchmark; stochastic planning can require native recovery; a failed gate
can leave the root blocked rather than automatically failing its ancestors.
These are not silently counted as fixed.
