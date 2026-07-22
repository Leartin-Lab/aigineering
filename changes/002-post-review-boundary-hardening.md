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
- Session/trace paths validate protocol identifiers before filesystem access.
- Malformed Worker packages and model tool arguments fail with typed protocol
  errors; invalid tool JSON is never converted into an empty invocation.
- Claim-renewal failure now closes the attempt durably instead of leaving an
  active claim until timeout.
- Planning refuses later children when the parent's remaining allowance is
  zero, preserving aggregate fan-out containment.
- Legacy and scaffold planning share task-quality gates: non-empty descriptions
  and outputs, valid activation grammar, accepted-child input reachability, and
  complete parent-output recommitment.
- Final plan reachability is derived only from accepted producers; rejected
  sibling promises, ungrounded cycles, and self-dependent tasks cannot enter a
  graph that will wait forever. Compile examples adapt to the actual allowance.
- Planning labels expose exact `/exec` schemas, causal allowance, and a
  Contract-specific valid compile example; nested invocation parameters are
  thawed before canonical serialization.
- Recovery replay records `recovery_unavailable` when publication is rejected,
  never a nonexistent scheduled recovery. Stable phase/validation-field codes
  preserve diagnostics without model or provider text.
- Claim-bound Candidate usage metadata is projected onto the owning task trace.
- Invalid activation punctuation is rejected at admission; historical invalid
  expressions project an explicit blocker and descendant risk.
- Large orchestration owners were separated without adding new ingress:
  EngineWorker setup/bridge/loop/output collection, CLI once/target loops,
  claim routing, and SQLite commitment substeps now have narrow helpers.
- FastAPI uses one request-scoped Store connection for validation and commit,
  then closes it deterministically.

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
Final live acceptance additionally completed an exact 20-task serial chain and
two nested replan cycles with no rejection, recovery, or silent-failure risk.
