# Native guarded claim review — 2026-09-13

The guarded workflow repaired the unchanged historical draft from
[report 063](063-runtime-native-cases-2026-09-13.md), passed deterministic structural
checks and separate model review, and qualified the exact revised report through
a dedicated signed acceptor. This is a bounded composition example, not evidence
that arbitrary claims or plans are reliable.

Implementation: v0.5.11 development work based on `f99d8a7`, with
[change 024](../changes/024-guarded-review-closure.md) and
[ADR-025](../docs/adr/ADR-025-guarded-review-closure.md). The
[checked-in example](../examples/guarded-claim-review/README.md) uses DeepSeek
`deepseek-flash` at the configured official endpoint.

## Execution boundary

Each run imported its inputs into a fresh domain, created one root with ordinary
CLI commands, and ran the configured Fleet. The root requested `/plan`; native
draft, dependency analysis and compile work produced the five business children.
There was no script creating those children, no second scheduler, and no edit of
model-produced Assets between stages. Setup and read-only evidence export used
CLI/API orchestration only. All input data are synthetic.

The first three frozen inputs are the research source, original draft and actor
policy. Later attempts also disclose an immutable interface document directly to
model-backed stages. This prevents field schemas and required inputs from relying
only on lossy restatement by a planning draft. The successful workflow contains
one revised report generation. A planning failure recovered through an ordinary
new task; no report revision loop ran inside a Worker.

The acceptor recomputes both structural checks, validates gate and semantic
receipts against exact Asset IDs, and checks committed producers. It alone has
`asset.attest` and the policy's `claim.review.accept` authority. A literal quote
binding proves source membership, not entailment or complete rationale support.

## Preserved development attempts

| Attempt | Observed result | Evidence |
| --- | --- | --- |
| Routed prototype | Report caused mechanical compile completion, cancelling gate and semantic children; root timed out unqualified | [routed](data/065-guarded-claim-review/repair-routed.json) |
| Acceptance obligation retained | Gate ran and rejected `excerpt_text` instead of required `quote`; root timed out unqualified | [guarded](data/065-guarded-claim-review/repair-guarded.json) |
| Frozen report interface | Structure and semantic review passed; strict target-binding presentation rejected the acceptor description | [interface](data/065-guarded-claim-review/repair-frozen-interface.json) |
| Duplicate source claim ID | Gate rejected the frozen source as unrepairable; no passing gate receipt or root qualification | [unrepairable](data/065-guarded-claim-review/unrepairable-source.json) |
| Input omission | Planner omitted source/draft/check inputs from gate; adapter rejected it before checking | [omission](data/065-guarded-claim-review/repair-verified.json) |
| Exact stage interfaces | Two planning failures recovered; structure and semantic review passed, but a literal target placeholder blocked the acceptor | [placeholder](data/065-guarded-claim-review/repair-exact-interfaces.json) |
| Unique exact target ID | Root completed and exact report qualified after one ordinary planning recovery; audit exposed an active intermediate compile task | [qualified](data/065-guarded-claim-review/repair-final.json) |
| Atomic closure correction | Root complete; 8 complete, 1 failed planning draft, 1 cancelled compile, zero active Contracts; exact report qualified | [final closure](data/065-guarded-claim-review/closure-final.json) |

These are evolving development configurations, not repeated samples of one frozen
release or a statistical success-rate estimate. Every model run, including failed
configurations, is retained. A Fleet `timed_out` result means the root remained
active and unqualified after its 180-second observation window; it does not mean
the root became terminal `failed`. Failed stage and failure-report facts identify
the explicit blocker.

## Review of the qualified report

The `repair-final` and `closure-final` reports preserve all three original claim texts. They label the
observed comparison `supported`, the causal upgrade `overstated`, and broad
population generalization `overstated`. Their revisions use association and group
means, preserve the two-campus limitation, and cite limitations when used in the
third rationale. All explicit bindings are literal substrings and match the cited
excerpt sets. Manual inspection agrees with the model reviewer for this synthetic
sample; it is not an independent empirical study of review accuracy.

## Defects found and fixed

- Compile stages now retain the parent's full acceptance policy, rather than
  finishing mechanically before required verification can run.
- Attestation compilation accepts the immutable tuple representation of JSON
  evidence/rubric arrays. Nonempty evidence lists previously failed before
  commitment despite being valid on the wire.
- The acceptor reads one unique canonical target Contract ID from signed task
  intent, without requiring an extra model-generated JSON wrapper. The runtime
  still validates the exact target, ancestry, evidence and authority.
- Independent completion now reuses the ordinary reducer's unfinished-child
  cancellation in the same transaction. SQLite regression evidence verifies that
  a compile obligation is cancelled when its original root qualifies and that
  reconstruction preserves the resulting state.

The earlier snapshots remain unchanged, including the active intermediate task
in `repair-final`. They are not rewritten to show the later correction. A fresh
`closure-final` live run confirms the corrected atomic cancellation, with no active
Contracts left; one planning failure recovered through an ordinary successor.

## Evidence and limits

Each JSON export contains exact Contracts, selected Assets, Candidate and terminal
records, usage/audit projections, and reconstruction fingerprints. Reconstruction
runs on private database copies; only JSON evidence is published. All eight
stores rebuilt with equal before/after digests and unchanged durable records.

Those eight runs contain 44 recorded usage-bearing responses totalling 232,755
reported tokens, plus four failed model responses whose usage did not survive
Candidate compilation. Those tokens are therefore a recorded subtotal, not a
billing total. Failure raw outputs and diagnostic categories remain observable.

A live successful root proves this composition can run. It does not prove robust
planning, semantic correctness in general, automatic recovery from every blocked
workflow, or v0.6.0 readiness. Missing/wrong receipts, wrong producer, ambiguous
targets, excessive revision depth and insufficient actor authority also have
focused deterministic and signed SQLite coverage.

Local verification: Ruff lint/format passed; full suite 1428 passed, 3 skipped.
