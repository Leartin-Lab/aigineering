# ADR-013: Causal allowance is an immutable task-lineage fact

Status: Accepted
Date: 2026-07-19
Related: ADR-010, ADR-011

## Context

`Contract.budget` previously served both as a declaration and as mutable runtime
state. Process-local counters cannot prove recursive containment after restart
and two publishers can both pass the same preflight balance.

## Decision

A root Contract declaration creates `allowance.granted`. Publishing a child
creates one `allowance.reserved` against its parent and a matching grant for the
child. A terminal Contract creates `allowance.extinguished` for its remaining
amount. These content-addressed RuntimeRecords are the runtime authority;
`Contract.budget` supplies the root declaration.

Projection rejects a Candidate batch whose total reservation exceeds the fact
snapshot. SQLite repeats reservation and terminal-extinguishment validation
inside the write transaction. Exact Candidate replay is idempotent and cannot
create a second reservation.

Planning, execution, and verification are recorded as reservation purposes for
audit and future policy. v0.5 does not introduce Worker accounts, prices,
transferable balances, or a compute market.

## Consequences

- Runtime allowance is reconstructable without Engine memory.
- Recursive task publication cannot amplify its parent's grant.
- Concurrent SQLite publishers have one transactionally ordered result.
- Cancellation consumes the unreserved remainder; recovery that needs work must
  reserve its replacement before extinguishing the source.

## Evidence

- `tests/architecture/test_causal_allowance.py`
- `tests/architecture/test_staged_planning.py`
- `tests/architecture/test_commitment.py`
