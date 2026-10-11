# ADR-043: Edit task instructions through immutable handoff successors

Status: Accepted; bounded development verification recorded in report 086
Date: 2026-10-05

## Context

Operators need familiar title and body editing while work retains signed,
auditable identity. Mutating an existing Contract would change a claim's scope,
frozen inputs and replay meaning. A separate edit scheduler or publication owner
would duplicate the existing bounded task handoff boundary.

## Decision

Use the existing sole `task.handoff` effect with an optional strict `edit`
object. It contains one or both replacement `title` and `body`, the exact source
assignment head (or empty string), and the sorted unique complete set of open
source hold IDs. Each text is valid UTF-8 and bounded to 65536 bytes; title is
nonblank and body may be empty. Reject canonical Unicode NFC no-ops, unknown
fields and invalid snapshots. Edit cannot combine an assignment override or
an additional hold reason. Omitting `edit` preserves ordinary handoff behavior.

The handoff plugin cancels and fences the source through its existing terminal
and claim facts, then declares a new bounded successor atomically. Requested
text replaces its field exactly; omitted text remains the source value without
a suffix or appended reason. The reason stays in the attributed transfer audit.
Existing remaining-output, causal-allowance, frozen-input, capability, trust,
disclosure and acceptance-policy owners bound the successor. Qualified old
outputs remain exact audit references, not new input or acceptance evidence.
Assignment inherits through its existing owner; priority does not inherit.

Edit retains ordinary eligible business-root, active-claim and expanded-tree
handoff support and exclusions. Only source holds are allowed on this inventory
path; descendant holds still block transfer. Snapshot at most 64 source holds
and atomically copy each exact reason to a new successor placement. Do not
release or rewrite the old placements. The successor is never committed without
its copied holds. An operator must explicitly unblock some source placements
before editing a task with more than 64 open holds.

Edit Candidates add the canonical source declaration, nonempty assignment head
and source hold IDs to their causal parents. Copied placements bind the receipt,
old placement and successor declaration. The existing `task.handed_off` link
records supplied field names, declaration references, exact source snapshot and
a one-to-one old/new hold map. Full replacement text stays in Contract
declarations. There is no separate edit ledger or version counter.

Require explicit `task.edit` in addition to ordinary handoff cancel/publish
authority, and `task.block` when copying holds. A dedicated local edit operator
does not widen existing handoff-only keys. Generic writer-locked reprojection
rechecks the exact snapshot and current authorization; the ordinary generic
hold placement and claim boundaries remain authoritative.

Exact replay authenticates the original Candidate, receipt, committed head and
ordered members before mutable source checks. Declaration and copied-placement
provenance must match. Later old-source hold cleanup cannot change the original
successor, and current key authorization remains required. Generated allowance
coeffects may follow the handoff link in the full committed member list.

## Consequences and alternatives

`task edit` returns the full successor ID, source terminal state, actual replay
key, exact source assignment/hold snapshot and copied hold mapping. `task edits`
filters the shared durable handoff history. Read-only history never completes
work. Redis remains required; accepted SQLite facts survive a pending relay and
the CLI reports their retained result explicitly.

In-place mutation, a second edit effect owner and sequential hold copying were
rejected because they weaken immutable scope, shared publication or atomic
claim fencing. Requiring operators to reconstruct a handoff by hand would add
avoidable scope and hold-copy errors. This decision adds no kernel plugin
branch, Store table, schema or reader floor. Development verification and stable
release qualification are separate gates.

See [expanded handoff](ADR-035-expanded-root-handoff.md),
[task assignments](ADR-038-transactional-task-assignments.md),
[direct fact ownership](ADR-040-injected-direct-fact-ownership.md) and
[exact hold batches](ADR-042-exact-hold-set-promotion.md) and
[development verification](../../reports/086-task-edit-successors-2026-10-06.md).
