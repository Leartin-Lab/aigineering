# ADR-012: Authenticated worker coordination commands

Status: Accepted
Date: 2026-07-19
Scope: HTTP Worker claim and lease renewal in v0.5
Related: ADR-002, ADR-006, ADR-009, ADR-011

## Context

Worker submission already proves possession of the registered actor key and
binds output to a claim, package, lease, epoch, and idempotency key. HTTP claim
and renewal previously trusted a `worker_id` reported in an unsigned request.
That asymmetry allowed another caller to reserve or prolong work under a
registered Worker identity even though it could not submit a valid result.

## Decision

HTTP claim and renewal accept complete actor-signed Candidate proposals:

- `worker.claim` binds Worker, optional Contract, lease request, and a unique
  idempotency key;
- `worker.claim.renew` additionally binds claim ID and fencing epoch;
- the Candidate actor/key must match one enabled Worker registration and hold
  the existing `worker.submit` protocol capability;
- Candidate receipt and the operational request record commit in the same
  SQLite transaction as `claim.granted` or `claim.renewed`;
- an already committed command Candidate cannot be replayed to extend a lease.

These operational effects are handled by the Worker coordination service. They
remain unsupported by the generic fact committer, so they cannot bypass claim
arbitration or become ordinary mutation effects.

## Consequences

Remote Workers must sign claim, renewal, and submission commands with their
registered key. An API client that only knows a Worker ID cannot lock work or
probe a claim epoch through the renewal result. Local in-process coordination
may call the Store port directly because it already holds the WorkerHost
identity and does not cross the HTTP trust boundary.

The reference server still expects deployment transport protections for
confidentiality and availability; this ADR establishes actor authentication,
not a complete internet-facing security posture.

## Verification

Server tests cover cross-replica signed claim/renew/submit, unsigned and
tampered rejection, registration binding, and command replay rejection. Store
tests continue to cover epoch, identity, expiration, and transaction fencing.
