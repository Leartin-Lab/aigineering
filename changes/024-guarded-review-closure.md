# Guarded review through native planning

Status: Implemented

The guarded claim-review example combines a deterministic draft assessment,
one model-backed revision generation, a deterministic structural gate, a separate
model-backed semantic review, and a dedicated acceptance Worker. The operator
imports frozen inputs and creates one root; ordinary staged `/plan` work compiles
all business children. Workers return Candidates through the existing host.

Only the configured acceptor holds attestation authority. It recomputes structural
checks and binds the source, draft, policy, report and receipts to their exact
committed Asset IDs and distinct configured producers before attesting the root.
Missing, mismatched or negative receipts cannot qualify that output. Structural
checks prove literal bindings, not semantic truth. Runtime retries remain visible
attempts within one revision generation; this is not a recursive repair controller.

Composition testing also exposed generic defects: planning compile stages
now retain the parent's full acceptance policy, preventing early completion from
cancelling required verification children; attestation compilation accepts the
immutable tuple representation of wire-array evidence and rubric references.
The wire format remains JSON arrays and qualification still checks exact policy.
Independent qualification also shares the ordinary completion path's child
cancellation consequences within the same transaction.

See [ADR-025](../docs/adr/ADR-025-guarded-review-closure.md) and the
[example](../examples/guarded-claim-review/README.md). No Store schema migration
or claim-specific kernel effect is introduced.
