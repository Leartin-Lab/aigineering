# Change 118: Index the existing committed-Candidate lookup

## Problem

The SQLite writer checks whether a Candidate is already committed using its exact
Candidate ID. The existing record-type/revision index selected all committed heads
before evaluating the payload ID. Each later publication therefore incurred a
historical SQL scan even when that Candidate had never been seen.

## Resulting design

A non-unique partial expression index on the existing runtime-record table keys
`json_extract(payload_json, '$.candidate_id')` for `candidate.committed` records.
The original lookup, authentication order, commitment transaction and replay
semantics are unchanged. This is a SQLite query mechanism, without a new table,
fact type, authority source or application cache.

SQLite maintains the index with the original insert/rollback. Writable opens
create it for existing supported Stores through the normal schema initialization;
this cold build scans history and has sorting/construction cost. Read-only opens
do not add it. The schema reader floor remains 21 because existing statements and
fact meanings remain compatible, including writes from already-open connections.

## Public compatibility

There is no CLI, protocol, Store capability or task behavior change. Rejected
Candidates remain audit-only and are not committed heads. The predicate remains
an existence check, without imposing a new uniqueness rule on historical heads.
This fixes one SQL hotspot; full projection, allowance, Asset propagation and
maintenance may still scan history. It does not close the overall quadratic
publication defect or qualify long-running workspaces.

## Required verification

Use signed publication to construct at least 64 committed heads. Compare the
missing-ID query plan and SQLite VM instruction count before/after indexing;
VM instructions measure database work, not exact rows or wall time. Verify
committed, rejected and empty IDs, exact replay through a second connection,
and index creation on writable reopen without changing fact IDs or reader floor.
Retain authentication-rejection, commitment, SQLite and full-suite gates.

## Closure

The expression index localizes the committed-Candidate lookup without changing
identity or replay semantics. Cold index construction and unrelated historical
consumers retain their separate costs; this change does not establish whole-run
scaling or task delivery.
