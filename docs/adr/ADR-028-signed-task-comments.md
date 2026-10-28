# ADR-028: Represent human comments as signed task facts

Status: Accepted for the local development implementation
Date: 2026-10-04

## Context

Task inspection needs a durable conversation without treating comments as
business outputs, acceptance evidence, or scheduler state.

## Decision

The `task.comment` Candidate effect requires its own capability and declares an
exact Contract, bounded UTF-8 text, and an optional committed same-task reply.
The collaboration plugin projects an immutable `task.commented` RuntimeRecord.
Its actor and key come from the authenticated Candidate. Unknown payload fields,
missing targets, and cross-task replies are rejected.

The CLI authorizes a dedicated local collaboration actor through the existing
signed root authorization path. Ordinary Worker submission authority does not
grant comment authority. Claim-bound Worker results cannot contain this effect.
The application explicitly injects a frozen plugin effect registry into the
generic publisher/committer. Additional handlers cannot override built-ins;
without that configuration the core rejects the effect. The core does not
import the collaboration plugin. The existing commitment transaction remains
the only durable write boundary.

Comments may be appended after a terminal outcome. They do not alter Contracts,
Assets, output obligations, claims, allowance, acceptance, or task status. Redis
relays metadata; comment content remains in the SQLite facts. Post-commit Redis
failure reports retained facts and pending synchronization.

## Alternatives and consequences

Publishing comments as business Assets risks satisfying output obligations.
Editing task rows or keeping a separate conversation database adds another state
owner. Typed signed facts retain attribution and reconstruction while keeping
collaboration semantics in a plugin.

An idempotency key participates in Candidate identity: replaying the same signed
input returns the existing comment. Changing text, target, or reply creates a
different input even with the same key. This is not a globally unique message-key
constraint. A comment is a statement by its signing actor, not verified truth or
independent acceptance.
