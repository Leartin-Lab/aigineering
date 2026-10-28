# ADR-029: Arbitrate signed claim holds in SQLite

Status: Accepted
Date: 2026-10-04

## Context

An operator needs to prevent an unclaimed task from starting while resolving an
input or review question. A displayed blocked card cannot provide that guarantee
unless the claim transaction observes the same facts.

## Decision

The collaboration plugin owns `task.block` and `task.unblock`, each requiring its
own explicitly authorized capability and a single-effect Candidate. It produces
one generic `claim.hold.placed` or `claim.hold.released` fact. The placement record
ID identifies the hold. Reasons are bounded UTF-8 text; attribution comes from
the authenticated Candidate. Release references exactly one same-Contract hold.
There is no parallel mutable card state or authoritative hold table.

The core owns the generic claim barrier, its pure reducer and transactional
validation. Both claim acquisition and hold commitment inspect facts inside
SQLite's writer transaction. A placement cannot commit after a claim has won;
a claim cannot commit while any hold is open. Release rechecks the exact open
hold under the same lock. Receipt and committed-envelope provenance are bound
to the fact, Candidate, actor and key. Unsupported schemas fail closed.

Multiple holds remain independent. A `task.unblock` capability permits a domain
operator to manage an exact hold, including one created by another authorized
actor. It does not infer authority from a caller-supplied author or role.

Placement refuses tasks with terminal or claim history. Existing claimed work
is never reset. A terminal outcome remains dominant; an old hold may be released
for audit cleanup without reopening the task. Projectors recognize exact
previously committed Candidates before checking current state, so replay after
release, claim or terminal closure retains the original fact.

The application injects plugin handlers through the existing frozen registry.
Core modules do not import the collaboration plugin. Redis relays metadata and
wakes Workers; it has no authority over holds or claims.

## Alternatives and consequences

Checking only the task view permits a concurrent or direct claim to bypass the
block. Resetting a claim violates attempt fencing and retry provenance. A second
mutable hold database creates another authority owner. A fact-derived barrier
with checks at both SQLite boundaries provides a replayable control operation.

This decision covers operator holds before claim acquisition. Pausing an active
Worker or returning work for changes requires an ordinary successor/handoff
workflow; rejecting a new hold on claimed work does not implement that workflow.
