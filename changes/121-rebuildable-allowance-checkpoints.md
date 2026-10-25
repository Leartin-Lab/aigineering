# Change 121: Add rebuildable allowance checkpoints

## Problem

Allowance balances are projections of signed grant, reservation, return and
extinguishment facts. Repeated balance work can revisit the same historical
reservation prefix. Before a later change consumes cached balances, SQLite needs
a bounded and auditable checkpoint lifecycle that preserves the existing
reference calculation.

## Resulting behavior

The pure allowance module now folds raw integer components without applying
budget fallback or clamping. It preserves record-ID replacement, distinct fact
counting, and both sides of a return where the source and donor are the same
Contract. Unconvertible matched history is opaque.

SQLite creates a private versioned checkpoint table without raising the schema
reader floor. Newly inserted balance facts update affected distinct accounts in
the caller's transaction; checkpoint rows store canonical decimal text, a
global revision cursor and its basis record ID. Refresh reads only the indexed
related tail after a valid cursor. Duplicate record replay is unchanged, while
older/reference-mode writers are detected through that same tail on the next
read. The read path does not write or warm the cache. Missing cache state falls
back to a read-only reference fold; opaque or malformed cache state is not
interpreted as zero and remains reference-only until explicit rebuild.

Cache-only rebuild and audit operate on derived rows. Rebuild takes a writer
transaction only when it owns the transaction and never closes an outer one.
The existing runtime materialization rebuild also reconstructs checkpoints from
its captured immutable prefix. Audit reports structural or prefix mismatches
without changing facts. The semantic materialization digest ignores checkpoint
physical state and refuses invalid valid-status assertions.

## Verification and scope

The final author recheck passed 82 focused tests in 2.32 seconds, followed by 2
schema-migration tests in 0.07 seconds. The focused run includes the raw fold,
checkpoint lifecycle, write hooks, snapshot consistency, compatibility and
architecture checks. Its measured relation matrix covers all five indexed
relations, tail sizes 1 and 2, relevant-history sizes 0/16/64/256, and unrelated
history sizes 0/16/64 (120 cases). For each case, test-side returned, decoded and
folded rows equal the fixed tail size; raw SQL rows are reported separately for
each relation. The plans use the relation index with both source and revision
range constraints. SQLite VM callbacks stay within the test's frozen bound; they
are a database-work proxy, not an exact count of scanned rows or end-to-end
runtime steps. The raw-total regression also verifies that 4,300-digit integer
amounts survive checkpoint refresh and rebuild; it is a storage/fold pressure
case, not evidence that such a multi-grant business transaction is authorized.

The author retained an earlier red for the two-connection reference-read race:
the totals came from before a concurrent commit while the reported revision
included it. The final focused run includes the corrected same-snapshot behavior.
Earlier implementation failures and their diagnostics remain in the private
development evidence; this record reports the final author run only. These are
author checks, not independent review. No consumer integration, end-to-end
performance improvement, full suite, installed artifact, or provider execution
is claimed here. Redis-backed coverage is not claimed by the final focused run.

## Limits

C3a adds checkpoint maintenance but does not connect a balance consumer. It does
not change allowance decisions or deliver an end-to-end speedup claim. Repeated
writer validation of same-parent reservations, publication proof costs,
allowance return/terminal consumers, and the wider C performance goal remain
open for separately reviewed work.
