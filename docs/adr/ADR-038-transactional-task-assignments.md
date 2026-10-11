# ADR-038: Bind exact tasks to Workers at the claim writer

Status: Accepted
Date: 2026-10-05

## Context

An operator needs to select a Worker without rewriting a frozen Contract or
transferring an existing claim. A displayed assignee or scan filter cannot
prevent a concurrent claim through another entry point. Successor work also
needs to preserve that restriction without exposing an unassigned interval.

## Decision

The assignment plugin projects a sole signed `task.assign` effect requiring
`task.assignment.manage`. Initial set, replacement and clear produce the same
reserved fact, `claim.assignment.changed`, with exactly seven payload fields:
`actor_id`, `candidate_id`, `contract_id`, `key_id`, `previous_assignment_id`,
`reason` and `worker_id`. Empty Worker ID clears the binding. The newest fact
remains the head after clear; only a task with no history has no predecessor.
The exact fact ID is also the operator-facing assignment ID.

Placement requires a known live task, no claim history and no satisfied
nonempty output obligation. Outputless tasks are not vacuously complete.
The Worker must be enabled and statically compatible with the Contract's
capabilities and pools. A busy compatible Worker can receive queued work;
capacity is checked when claiming. Assignment binds stable Worker ID without
permanently pinning a registration version. Operational claims retain the
current registration, actor/key and signature checks from
[ADR-037](ADR-037-transactional-worker-authentication.md).

One pure core owner validates history, routing restrictions and reserved-fact
provenance. SQLite checks assignment publication and claim acquisition in their
writer transactions. All claim history counts, including released, submitted
and legacy trusted claim rows. A held task may be assigned; an old claim cannot
be reset or transferred by reassignment. Assignment changes no Contract identity,
budget, tools, disclosure, capability authority or output qualification.

Handoff and review-linked revision inherit the effective Worker atomically with
source cancellation and successor declaration. Explicit replacement or clear
requires additional `task.assignment.manage` authority and a signed
`assignment_override` object containing exactly `source_assignment_id` and
`worker_id`. The source reference includes a clear head; empty is valid only
before any history. Clear of an unassigned source rejects. Set on a source
without history is valid and creates the successor's first assignment.
Default unassigned/cleared inheritance emits no assignment fact. No-history
default successors retain their previous effect and link identities.

Frozen injected-handler permissions and core-generated fact ownership bind the
source head, exact pending cancellation, child declaration, assignment and link
to one effect projection. The typed witness is revalidated with current facts
and actor keys under the writer lock, and its identity participates in snapshot
comparison. The link references the assignment fact; the fact does not reference
the link. Ordinary children do not inherit assignment merely through parent ID.
The core does not import handoff or revision implementations.

Exact signed replay authenticates the Candidate, proves its unique committed
head, receipt and complete record membership, then returns the original
transition before mutable head, claim, output or Worker eligibility checks.
Historical registration parents remain immutable evidence even after rotation
or disable. Reusing only an idempotency string with changed signed intent is
not replay. Incomplete or conflicting provenance rejects observably.

## Compatibility and consequences

Schema21 is the normal reader floor before the first assignment. Stop old
Workers and use `domain upgrade` to retain a pre-upgrade backup. Genuine schema20
facts keep their IDs; pre-21 assignment rows refuse migration. The marker cannot
revoke already-open old connections. MemoryStore supports pure/control
publication without acquiring an operational claim API.

Redis collaboration uses schema-v2 derived event metadata with typed `worker_ids`
and domain-scoped rebuild. Causal ancestry can include previous assignees; these
references describe event attribution, not a task's current assignment. SQLite
remains the fact and claim authority. An outage
does not undo accepted facts; synchronization pending remains visible.

A mutable scheduler field would create a second state owner. Scan-only filtering
would leave a claim bypass. Assigning a successor after its declaration would
leave an unassigned window. Permanently pinning registration would obstruct key
rotation. Priority, bulk mutation and automatic assignment to ordinary descendants
remain separate capabilities. This decision does not qualify a stable release.
