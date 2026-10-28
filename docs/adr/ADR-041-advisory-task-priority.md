# ADR-041: Signed priority history orders application claim scans

Status: Accepted
Date: 2026-10-05

## Context

Operators need to favor urgent work through the familiar CLI. A priority value
must not become a second queue, mutable task status or a new claim authority.
Existing applications scan ordinary Contracts; SQLite already arbitrates
eligibility and claims. Several target-tree and engine paths enumerate tasks
before making individually pinned claim requests, so a broad-scan change alone
would leave inconsistent behavior.

## Decision

Use the plugin-owned single effect `task.priority.set`, requiring
`task.priority.manage`, to publish `task.priority.changed`. The effect carries
`contract_id`, `priority`, `previous_priority_id` and `reason`; its fact adds
`actor_id`, `candidate_id` and `key_id`. The handler declares and reserves its
fact type through [ADR-040](ADR-040-injected-direct-fact-ownership.md).

Priority is an integer from -100 through 100, excluding booleans. Larger values
are considered first. Zero means the default or a clear; a task without history
has effective priority zero without a synthetic fact. Clearing retains the
history head. Every subsequent mutation names the exact current head, including
after a clear. No-op changes, malformed or forked chains and new changes to
terminal or canonically satisfied output obligations are rejected. An existing
claim continues unchanged; priority does not reset or transfer it.

The CLI uses `task priority ID INTEGER --reason TEXT` with an explicit full
`--previous` after any history exists. `task priorities ID` exposes copyable head
and history identifiers. Explicit idempotent replay must prove the same signed
Candidate with a currently valid actor key; matching an old value and reason
alone cannot bypass current-state validation or provision a new operator key.
The writer rechecks the exact current chain before any mutation. Redis remains
required operational infrastructure, while SQLite retains committed facts if
post-commit relay is pending.

One application function orders a supplied candidate ID sequence by priority
descending, original declaration revision ascending and full Contract ID
ascending. It only reorders the supplied candidates. Runtime accepts a generic
optional full-permutation callback after capability and assignment routing;
original Contract objects and all readiness and transactional claim checks stay
authoritative. An explicitly pinned claim bypasses the callback. Application
target-tree and engine enumeration use the same ordering function before their
existing pinned attempts and preserve their original scope. No queue or mutable
dispatch state is introduced.

Application task views expose effective priority. Explicit priority sorting is
available without changing their existing default display order. Successors,
retries and ordinary children do not inherit priority; operators assign each
new Contract deliberately.

## Limits and alternatives

This is advisory ordering within a scan snapshot. Concurrent processes may see
different priorities or win claims in a different order. There is no global
queue order, fairness, aging, start-time or mixed-version ordering guarantee;
high-priority arrivals may starve other work. Schema21 remains unchanged and
older readers may ignore the hint. These limits justify retaining the reader
floor for this feature rather than treating the value as an execution fence.

Redis ranking as claim authority would add another arbitration boundary.
Putting priority range or history interpretation in Core would place a feature
in the kernel. Changing Contract identity would conflate an operator hint with
the frozen obligation. A wrapper plus generic permutation hook keeps these
responsibilities separate and covers both broad and scoped application scans.

This ADR selects the behavior. Implementation, every-entry integration and
release acceptance require separate evidence.
