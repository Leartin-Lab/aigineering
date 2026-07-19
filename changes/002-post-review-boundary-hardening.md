# Change 002: Post-review boundary hardening

Status: Implemented and verified
Target: 0.5.0 stable
Public decision: `docs/adr/ADR-012-authenticated-worker-coordination.md`

## Problem

A post-review pass found adjacent paths where the implemented runtime was
weaker or less consistent than its public invariants: unsigned HTTP claim and
renewal, split completion-marker persistence, conflicting terminal trace
emission, non-atomic mixed planning fan-out, method-result provenance drift,
and expensive or duplicated diagnostic projection work.

## Implemented change

- HTTP Worker claim and renewal are signed, registered-key-bound, single-use
  operational Candidates whose authentication records commit with the lease
  transition.
- Completion projection commits its audit marker and durable projected marker
  in one Store batch.
- Continuation completion checks any existing terminal and atomically records
  terminal fact plus trace, preserving single assignment.
- Plan/replan fan-out treats `rejected` and `scaffold_rejected` diagnostics as
  atomic blockers and schedules recovery instead of retaining accepted
  siblings from a mixed invalid plan.
- Protected method-result Assets preserve explicit Contract provenance;
  FactReducer emits method-result events only for the Contract that declared
  that output.
- Recovery output satisfaction and FactReducer use the same origin semantics.
- CLI descendant risk projection reuses one Contract graph and RuntimeProjection,
  avoids recursive stack growth, and removes duplicate stalled/budget risks.
- Task loops construct the completion registry once per run.

## Compatibility

The HTTP claim/renew body is intentionally incompatible with the prior unsigned
shape. Custom remote Workers must construct `worker.claim` and
`worker.claim.renew` Candidate effects and use a fresh idempotency key for each
request. Worker submission wire semantics are unchanged.

## Exit criteria

- focused protocol, planning, terminal, reconstruction, and server regressions;
- architecture gates for auth ownership and atomic completion marking;
- full test, lint, format, build, metadata, and artifact-content checks;
- public and internal design/ADR/change/skill records updated together.

All exit criteria passed on 2026-07-19. Evidence is recorded in
`reports/050-post-review-boundary-hardening-2026-07-19.md`.
