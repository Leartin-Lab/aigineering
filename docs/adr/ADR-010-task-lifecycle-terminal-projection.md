# ADR-010: Task Lifecycle And Terminal Projection

**Status:** Accepted
**Date:** 2026-06-23
**Scope:** v0.5.0 delivery-blocking
**Related:** ADR-001, ADR-002, ADR-003, ADR-004, ADR-008, internal ADR-002,
ADR-003, ADR-010, ADR-018, ADR-021, ADR-036

## Context

The old runtime represented task lifecycle through overlapping private
Engine fields (`_completed`, `_suspended`, `_method_scheduled`) plus
trace entries.  These fields could diverge from durable records, making
it impossible to reconstruct the same lifecycle view after a restart.

The plan requires that control state be **reducible** — every lifecycle
state is a projection over durable facts (task declarations, worker
claims, candidate submissions, accepted assets, trace records, and
control facts). Production execution has no Engine-owned lifecycle fields;
legacy snapshot code is excluded from both release wheel and sdist.

## Decision

Task lifecycle is defined as a reducible projection over immutable
records.  No lifecycle state exists only in private memory.

### Lifecycle States

A task transitions through these projected states:

| State | Definition |
|-------|-----------|
| **declared** | A contract exists in the store. Not yet eligible for execution. |
| **claimable** | Contract activation is satisfied. Not yet claimed by a worker. |
| **claimed** | An active claim (`Claim.status=active`) exists for the contract. Worker holds exclusive lease. |
| **blocked** | Required Asset facts, capability, budget, or method-produced facts are absent; the boolean enabled predicate remains false. |
| **satisfied** | All declared outputs exist in the store. The contract is terminal. |
| **failed** | The contract encountered an unrecoverable error. Terminal. |
| **cancelled** | The contract was explicitly terminated or its parent completed while it was still unfinished. Terminal. |
| **unreachable** | The contract's activation can no longer become true (e.g., a required input asset has no active producer and no promise to create it). Terminal. |

### Monotonicity Rules

- **A claimed Contract is never reclaimed after failure.** A successful
  submission closes the claim; invocation failure or lease expiry appends a
  terminal fact. Retry or recovery creates a **new** Contract with linked
  context.

- **Output satisfaction completes a contract.** A parent contract is
  complete when its declared output asset names exist in the store.  It
  does NOT wait for all children to finish.

- **Blocked work requires output re-commitment.** If a plan/replan/method
  causes a task to be data-blocked, at least one newly created active task must
  explicitly promise every still-required output.  A task must never
  remain blocked where its declared outputs have no active
  promised producer.

- **Blocking requires input reachability.** A task must not depend on an
  input/activation dependency that is neither already satisfied, nor
  promised by an active producer, nor explicitly guarded by a condition
  that can exclude it.  Missing unguarded inputs trigger
  replan/fail/unreachable handling.

- **Replan appends branches, does not replace tasks.** Replan preserves
  the existing branch as historical/runtime fact and appends a new
  recovery/exploration branch.  Old branches are never marked replaced or
  superseded.  They can only become terminal through normal satisfied,
  failed, cancelled, or unreachable projection.

- **Condition branches may remain unfinished.** Boolean activation
  permits branches that never fire.  Once parent outputs are satisfied,
  remaining pending branches should be cancelled or marked unreachable,
  not allowed to block completion.

### Lifecycle vs Asset Replacement

Task lifecycle does not include a "replaced" or "superseded" state.
Asset replacement claims exist as separate immutable records — they
update catalog/disclosure views but never retroactively change which
asset satisfied a completed contract without an explicit signed override.

### Terminal State Projection

All terminal states (satisfied, failed, cancelled, unreachable) are:
- **Projected from durable records** — contracts, claims, assets, trace
- **Appended exactly once** — one immutable `lifecycle.terminal` fact exists per
  Contract; its audit trace is committed in the same transaction and cannot
  introduce a second, conflicting terminal
- **Replayable** — rebuilding from RuntimeRecords derives the same blockers,
  budget, claim head, and terminal view

## Consequences

### Positive

- Crash recovery reconstructs the exact same lifecycle view
- Lifecycle is auditable — every transition has a trace record
- Claim monotonicity prevents race conditions from reopening claimed work
- Waiting/decomposed contracts cannot deadlock silently
- Replan is additive, making audit and debugging of iterative work
  possible

### Negative

- Output re-commitment and input reachability checks make plan/replan
  more complex (scaffold validation must prove these invariants)
- Claim lifecycle requires creating new contracts for retry (more records)

## Verification

```bash
# Lifecycle reconstructed after restart
pytest tests/ -k "crash or restore or restart"

# Claimed contracts never become claimable
pytest tests/ -k "unclaimed or reactivate or back_to"

# Parent completion by output assets (not child execution)
pytest tests/ -k "parent_completion or output_satisfied"

# Replan appends branches, never replaces
pytest tests/ -k "replan.*branch or append.*replan"

# Terminal events idempotent
pytest tests/ -k "terminal.*exactly_once or double.written"
```
