# ADR-036: Gate task claims on exact prerequisite completion

Status: Accepted
Date: 2026-10-04

## Context

An operator needs to order existing tasks without changing their frozen inputs,
authority or output policy. A blocked card or Redis index cannot arbitrate a
concurrent Worker claim. A task dependency must therefore be a durable claim
restriction with a transaction-time check.

## Decision

The task-dependencies plugin projects a single signed `task.dependency.link` or
`task.dependency.unlink` effect, requiring `task.dependency.manage`. It produces
one generic `claim.constraint.placed` or `claim.constraint.released` fact. There
is no duplicate task-status fact or mutable edge table. The placement record ID
is the exact link ID. The core owns strict fact validation, open-edge reduction,
cycle checks, typed prerequisite status and claim blockers. Plugins call this
owner; the kernel does not import feature implementations.

The only supported predicate is `prerequisite_terminal`, with required terminal
`complete`. Placement binds exact target and prerequisite Contract IDs, reason,
authenticated Candidate actor/key and receipt. Release repeats the exact pair
and predicate, and references its own receipt plus the original placement.
Store admission binds the transition to its actual committed Candidate head and
rechecks current endpoints, claim history and graph under the writer lock.
Direct record append cannot publish either reserved fact type.

Placement requires a live target with no claim history and no satisfied nonempty
output obligation. Prerequisites may already be complete; that still creates an
auditable open, satisfied link. Unknown endpoints, self-links, duplicate open
pairs and cycles reject. Unlink requires the exact open placement and a live,
never-claimed target. A manual hold on that target permits these operations.
Identical signed inputs and key replay the original fact before current lifecycle
checks. Changed inputs produce a different Candidate and undergo fresh validation.

SQLite checks unresolved constraints in the same `BEGIN IMMEDIATE` transaction
that acquires a claim. Local and HTTP Workers share this gate. Failed, cancelled,
unreachable, missing or conflicting prerequisite terminals do not satisfy it.
Views use the same selected historical fact set; no-edge views preserve prior
projection behavior. MemoryStore retains pure/control-ingress support and has no
operational Worker claim API. Redis relays metadata and wakeups, never authority.

Dependencies gate new claims only. They do not change Contract identity, inputs,
disclosure, allowance, capabilities, output satisfaction or qualification.
Committed business outputs can still complete a task without acquiring a claim;
an open dependency cannot veto canonical completion.

## Replacement and successors

A dependency remains bound to its original prerequisite ID after cancellation,
retry, recovery or handoff. To replace it safely before claim, hold the target,
unlink the old exact placement, link the chosen new prerequisite, then release
the exact hold. These are separate signed decisions, not an atomic edit.

Handoff/revision refuses cancellation of a target that still depends on an
unfinished prerequisite outside the old cancellation set. Internal old edges
remain history rather than being cloned into a successor. A prerequisite completed
by the same authorized historical repair is satisfied; repair-only targets are
excluded because dependencies cannot suppress canonical completion. Preserved
branches retain their edges. Outside downstream targets keep a cancelled-old-ID
blocker and may be explicitly relinked; their incoming edges alone do not prevent
handoff. No dependency-based recursive cancellation is introduced.

## Compatibility and consequences

Schema20 is the mandatory normal reader floor, even before the first dependency
is published. Stop old Workers and back up with `domain upgrade`. Genuine older
facts retain their identities; pre-20 constraint rows refuse migration with the
old Store and upgrade backup retained. Already-open old connections are not
revoked by the version marker. Earlier input-binding and allowance-return gates
remain in force.

Application-only filtering permits direct claim bypass. Asset activation would
confuse ordering with evidence/disclosure, and automatic manual-hold release
would introduce another control owner. The fixed fact predicate keeps one graph
interpretation and one claim authority. Assignment, global priority, automatic
successor following and pausing OS processes are separate capabilities.
