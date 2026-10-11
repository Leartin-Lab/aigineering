# ADR-031: Bind task review to exact reports and the existing acceptance policy

Status: Accepted
Date: 2026-10-04

## Context

An operator needs to request review of a committed output, run ordinary reviewer
work, and record an acceptance or change decision. A report or display status
cannot become a second source of output qualification. Frozen policy evidence
also cannot retrospectively include a report that did not exist at declaration.

## Decision

The review plugin owns sole-effect signed `task.review.request` and
`task.review.decide` Candidates. Request creates a bounded ordinary reviewer
child and an immutable link to one exact output and its frozen policy. It needs
review-request and Contract-publication authority. Budget defaults to one current
source allowance unit; it is not token billing or a new root grant.

Reviewer context contains only exact target, rubric and evidence Asset IDs.
Publication bypasses name rebinding, and canonical disclosure must match the
original promptable context exactly. The child has no tools, delegation or
acceptance policy. It runs through the standard Worker claim/submission path.
Its description identifies the exact review target and asks for an ordinary
report. A separate `run --worker-id` selects a durable execution identity without
granting attestation authority; configured fleets retain configured identities.

Decision checks the exact committed report, reviewer ancestry and a report
producer distinct from the target producer. It delegates to the canonical
attestation projector with the original exact policy/rubric/evidence references.
The deciding actor needs review-decision, attestation and configured verifier
capabilities and must differ from the target producer. `accept` maps to an
accepted attestation; `changes` maps to a rejected attestation. The new report
is linked separately and is not appended to frozen policy evidence. Human
verdict is an authorized assertion; the report is not automatically interpreted.

One request admits at most one decision. Generic writer snapshot validation
rechecks pure injected projection before SQLite writes. Exact Candidate replay
restores original projection before mutable state and duplicate-verifier checks.
Qualification and terminal consequences remain owned by canonical acceptance.

The local decision default has fixed `verify.human` authority. Other policies
need an already-authorized actor/key and matching private key; selecting a
custom identity does not provision or widen capabilities.

## Consequences and boundaries

The current slice supports live independent-policy business sources. Ordinary
tasks cannot acquire a retrospective acceptance gate. Existing terminals remain
immutable. Review reserves source allowance and may leave insufficient funding
for a later revision. `changes` does not create revision work or reset a claim.
Expanded-tree transfer, ordinary advisory review and revision publication are
separate future slices.

Reviewer output names bind Candidate identity and reject preexisting collisions.
Ordinary report completion retains global same-name business-output semantics:
a concurrent foreign output may complete the reviewer task. That status does
not prove a valid review report. Decision independently rejects a foreign report
and cannot qualify source work with it. A valid exact reviewer report remains
eligible even if an unrelated same-name Asset also exists.

Distinct signed actors establish protocol attribution, not different natural
persons or independent models. Directly writing qualifications, changing task
status, using a report as newly frozen evidence, or granting arbitrary verifier
capabilities would bypass existing authority and acceptance owners.
