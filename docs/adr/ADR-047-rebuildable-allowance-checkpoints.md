# ADR-047: Rebuildable SQLite allowance checkpoints

Status: Accepted for the bounded SQLite cache lifecycle.

## Context

Allowance availability is derived from immutable grant, reservation, return and
extinguishment facts. C1 and C2 limit declaration projection inputs and index a
single publication proof, but repeated writer validation can still fold the
same reservation prefix. A future consumer needs a cache whose prefix, old-writer
tail, transaction maintenance and rebuild behavior can be verified separately
from any balance decision.

## Decision

Keep the reference allowance calculation in `causal_allowance.py` unchanged.
Add a pure raw fold that deduplicates by record ID and exposes the five integer
components plus grant count. It does not apply Contract-budget fallback or clamp
availability. An unconvertible matched amount makes the fold opaque; it is not
treated as zero.

SQLite owns one private `allowance_checkpoints` row per observed funding
Contract. The row stores versioned raw totals and a global runtime revision plus
the record ID at that prefix. Totals use canonical signed decimal text so Python
integer accumulation is not limited by SQLite numeric aggregation. The table
has no Contract foreign key and does not change the schema reader floor.

The SQLite writer refreshes each distinct affected account after a newly
inserted allowance fact, within the caller's transaction. Refresh folds only the
indexed related tail after a valid cursor; a missing row is built from the full
related prefix. Existing opaque or invalid rows stay reference-only until an
explicit rebuild. Replayed record IDs do not advance or double-count a row.
SQLite write errors propagate so the caller's fact transaction rolls back.

Raw reads combine the checkpoint and exact related tail in one read snapshot and
never warm or write the cache. An explicit reference mode bypasses operational
checkpoint reads and maintenance. Missing tables and rows remain cache misses.
Unknown or damaged metadata yields an opaque/reference-only result. SQLite I/O
and unknown programming errors continue to propagate.

`rebuild_allowance_checkpoints` replaces only the derived rows from one
immutable prefix. It takes its own `BEGIN IMMEDIATE` only when no caller
transaction exists; it never commits or rolls back an outer transaction. The
existing runtime materialization rebuild invokes it within its own current
transaction. `audit_allowance_checkpoints` is read-only and checks valid rows
against their exact fact prefixes. The semantic materialization digest rejects
invalid cache assertions but excludes cache rows from its semantic hash.

## Alternatives

Recomputing every account from the full log on every write preserves simplicity
but repeats the historical fold. SQL `SUM` risks numeric overflow and changes
the raw fact interpretation. Treating a cache row as balance authority would
make it a second source of truth. A process-local cache would not cover multiple
Store connections or restart recovery. C3a therefore persists only rebuildable
raw projection data in the SQLite adapter and leaves consumption to a separately
reviewed slice.

## Consequences

The immutable allowance facts remain the only authority. Checkpoint writes add
transactional cost to new allowance facts, and a cold miss or full rebuild may
scan the historical prefix. C3a does not change `allowance_available`, writer
authorization, projection decisions or rejection behavior, and it has no hot
path consumer by itself. It does not close writer-side same-parent P² work or
the overall C performance goal. C3b must bind any future balance consumer to a
consistent fact snapshot and compare its decisions with the existing reference
owner.
