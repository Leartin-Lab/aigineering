# ADR-025: Preserve acceptance obligations across native review stages

Status: Accepted and implemented

## Context

A produced report can be structurally well-formed and still be scientifically
wrong. Independent model review alone previously accepted reports with changed
claims and missing evidence bindings. Separate deterministic methods identify
those defects, but their assessments must participate in the acceptance path.

A native planning compile task previously used mechanical acceptance even when
its parent required independent verification. Publishing the report could complete
that intermediate task and cancel its gate and semantic-review children before
the root became qualified.

## Decision

Keep domain checks in business Workers. Freeze a source, an original draft and
an operator policy; disclose a frozen interface document to avoid lossy restatement
by planning stages. Compile five normal children: assess, revise, gate, semantic
review and accept. The reviser may consume the original draft and assessment,
not a previous revision. Model-backed stages invoke their delegate once per
attempt and may emit only their declared output or `/fail`.

The acceptor alone has `asset.attest` and `claim.review.accept` authority. Before
proposing `/attest`, it recomputes structural checks, validates exact receipt
contents and verifies their committed producer identities against frozen policy.
The target is one unambiguous canonical Contract ID in the signed child
description, because staged planning adds an intermediate parent. The
canonical commitment boundary independently checks the target policy, exact
evidence, producer separation and authority. A provenance seal is a consistency
check, not a replacement for authenticated Candidate provenance.

Planning compile stages retain the complete parent acceptance policy. Draft and
dependency-analysis stages remain mechanical. An unqualified report therefore
cannot prematurely finish compile and cancel verification. Qualification of the
original root closes that obligation; normal parent completion can cancel its
unfinished compile descendant. Independent completion reuses the same pure
child-cancellation reducer and commits those consequences atomically with root
qualification. No duplicate ancestor attestation is required.

## Consequences

The workflow has no business scheduler, hidden revision loop or Store mutation
inside a Worker. Failure reporting uses ordinary `/fail` child work. A failed gate
may leave the root blocked and unqualified until operator recovery; it does not
imply automatic ancestor failure propagation.

One revision bounds derivation depth, not total HTTP attempts under runtime
recovery. Literal quote checks cannot establish semantic entailment, complete
reasoning citations or causal validity. Model review can still err. The example
is evidence toward a broader runtime baseline, not a guarantee of scientific
correctness or a completed v0.6.0 release.
