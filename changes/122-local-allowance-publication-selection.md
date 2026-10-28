# Change 122: Select complete allowance publication proofs locally

## Problem

The allowance owner must inspect every competing committed head and matching
declaration to preserve its existing receipt, uniqueness, and funding checks.
For supported `contract.declare` writes, SQLite previously loaded broad history
to construct those inputs during projection and each writer validation.

## Resulting behavior

SQLite now maintains rebuildable committed-head membership relations and an
exact, non-unique declaration-pair index. Typed core requests select all
candidate heads, declarations, and the complete required receipt/member
closure from one snapshot. The original allowance validator still decides
uniqueness, funding, replay, source-terminal, and balance outcomes. Both
writer validations select fresh inputs in the existing transaction.

The optimized path requires a recognized pure declaration batch and known
canonical task identities. Pending records retain the original ID-overlay
semantics. Mixed or injected effects, malformed or opaque history, unsupported
identity, missing tables, invalid index metadata, and uncertain old-writer
state use the reference proof path. Index maintenance, audit, and rebuild are
derived SQLite operations and do not publish facts or create authority.

## Verification and limits

Author-side focused tests cover projection and both writer selections for
supported pure declaration batches, competing heads and direct receipt
parents, pending overlays, opaque/legacy fallback, index audit/rebuild,
bounded head-tail and declaration-index query work, reference-mode behavior,
non-head cursor behavior, and SQLite bind-variable limits. An earlier author
snapshot reports 67 focused behavior tests and 24 boundary/retry tests passed;
Ruff check and format checks pass. An earlier red attempt using a stale retry
fixture signature and the sandbox's default Redis endpoint remains recorded;
the fixture was updated narrowly, then its two compatibility regressions
passed against the dedicated test Redis. These results are author checks, not
independent acceptance.

Rebuildable selection indexes remain subordinate to exact publication proofs
and fresh writer checks. Independent connections must revalidate spending and
terminal changes; a derived index cannot grant allowance.

This slice localizes reads for the supported pure declaration projection and
its two writer-time validations. It does not optimize other runtime consumers
or the overall asset, claim, and sustained-workspace paths. Mixed or injected
effects, opaque or legacy history, unsupported identities, and replay cases
that cannot be safely narrowed retain the slower reference path.
