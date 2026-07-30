# ADR-010: Task lifecycle is a durable projection

Status: Accepted
Date: 2026-06-23
Related: ADR-001, ADR-002, ADR-003, ADR-004, ADR-008, ADR-011, ADR-013

## Context

Task progress must survive process restart and must not depend on private
Engine collections or an in-process call stack.

## Decision

Task status is derived from durable Contracts, facts, claims, allowance,
attempt outcomes, and terminal records.

| Projected status | Meaning |
| --- | --- |
| declared | Contract exists but required facts are absent |
| ready | activation and required facts are satisfied |
| claimed | one active fenced claim exists |
| blocked | a required fact, capability, or allowance condition is absent |
| expanded | the attempt published ordinary descendant work |
| completed | declared outputs are satisfied |
| failed | a durable terminal failure exists |
| cancelled | an authorized cancellation is terminal |
| unreachable | no accepted producer can satisfy the remaining obligation |

`blocked` is a Boolean consequence of the current facts, not stored waiting
state.

### Monotonic rules

- a claimed attempt is never returned to unclaimed;
- retry and recovery publish new Contracts;
- one Contract has at most one terminal fact;
- parent completion follows declared output satisfaction, not child count;
- replan appends ordinary branches and does not rewrite history;
- planned dependencies must be reachable from existing facts or accepted
  producers;
- unfinished work without a reachable producer is visible as failure,
  unreachable work, or an explicit risk.

Terminal fact and audit trace commit transactionally. Materialized task views
may be deleted and rebuilt from RuntimeRecords.

## Consequences

- any process can reconstruct and continue the same domain;
- claim fencing prevents stale attempts from reopening work;
- no enabled-work condition is mistaken for success;
- planning must prove output recommitment and dependency reachability.
