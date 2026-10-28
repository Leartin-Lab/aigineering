# ADR-042: Recover manually held work through an exact release batch

Status: Accepted
Date: 2026-10-05

## Context

An operator needs one recovery action for a task with several manual holds.
Sequential releases can leave partial recovery after a later failure. Task
readiness is derived from facts; writing a ready status would bypass other
eligibility owners.

## Decision

The collaboration plugin owns a sole signed `task.promote` effect requiring
`task.unblock`. It names one Contract, the complete observed sorted unique set
of open hold IDs, and a bounded reason. A fresh request requires a live task
with no claim history and no satisfied nonempty output obligation. Canonical
independent qualification remains part of output satisfaction.

The plugin emits ordinary `claim.hold.released` facts plus one `task.promoted`
audit, binding exact placements and releases to the authenticated receipt. The
audit is exclusively owned; generic hold facts remain shared direct outputs.
Writer-locked reprojection rechecks the complete set and eligibility. A changed
hold set rejects the entire task operation before any releases commit.

The generic hold validator permits at most 64 homogeneous transitions for one
Contract, rejecting mixed placement/release, cross-task batches and duplicate
records or release targets. Existing receipt/head, actor/key, current-open,
placement lifecycle and claim-history checks remain in the SQLite transaction.
The core does not recognize the promotion effect or maintain another hold ledger.

Exact replay proves the original signed Candidate and full committed-head
membership before mutable checks, while current key authorization still applies.
The CLI returns the actual idempotency key. Repeating the original explicit hold
IDs, reason and key restores the original action; later holds remain effective.
Automatic discovery selects a fresh hold snapshot, rather than guessing replay.

## Consequences and alternatives

Promotion records releases, not readiness, acceptance or completion. Dependencies,
activation, inputs, assignment, routing, capacity, allowance and terminal fences
remain authoritative. Results include observed status and remaining blockers.
Terminal hold cleanup stays with exact `unblock`; retry and recovery create new
work instead of reopening a claim.

The CLI accepts up to 64 task IDs, each with its own atomic Candidate and result.
Multi-task calls may partially succeed and return nonzero on any failure. More
than 64 open holds requires partial exact unblock while retaining a hold, then
promotion of the remaining complete set. Redis preflight and relay are required;
accepted SQLite facts survive synchronization outages. No schema or reader-floor
change is introduced.

A mutable ready field would create another authority. Unspecified all-current
release would widen signed intent. Sequential unblock cannot offer task-level
atomicity. A plugin branch in Store would duplicate feature ownership. The chosen
operation reuses the existing commitment and hold boundaries.
