# ADR-033: Publish bounded revisions linked to exact changes decisions

Status: Accepted
Date: 2026-10-04

## Context

A review decision requesting changes rejects an exact output under the frozen
acceptance policy. It neither resets a submitted claim nor creates executable
revision work. An operator needs a durable successor with the original scope,
remaining allowance and auditable connection to the rejected attempt.

## Decision

The revision plugin owns a sole-effect signed `task.revise` Candidate containing
`decision_id`, `reason` and nullable `budget`. It requires `task.revise`,
`contract.cancel` and `contract.publish`, without granting verifier authority.
A shared pure reference validator serves both application preflight and projection.
It binds the committed changes decision, its request, original policy, rejected
canonical attestation, exact target/report, report ancestry and distinct producers.

The Candidate atomically cancels the source, fences its claim and declares a child
successor using bounded current causal allowance. Every still-unqualified source
output transfers. Already-qualified slots and exact Asset IDs remain link metadata;
their contents are not automatically disclosed. Original execution requirements,
delegation, input roles, activation, tools and acceptance policy are inherited.
The operator's explicit reason becomes bounded Worker context.

Original input selections are snapshotted by the exact binding guard in
[ADR-032](ADR-032-exact-input-binding-reader-gates.md). Historical name-resolved
labels freeze their legally disclosed references. Exact original target and report
are the only additional context references; they do not widen frozen acceptance
evidence. Canonical disclosure must equal the original source's IDs and views
plus those two references. Later same-name input publication cannot widen it.

The successor owns all transferred output obligations and requires fresh
qualification under its original policy. The old rejected target cannot satisfy
the new Contract's lineage requirement. Successor completion never changes the
source's committed cancellation into completion.

A nullable signed budget resolves only on first projection. Exact Candidate replay
returns the original successor and resolved budget before mutable eligibility
checks. Generic writer-locked injected-decision revalidation arbitrates competing
state changes; a changed projection is durably rejected rather than substituted.
Shared successor helpers also serve handoff, preserving its existing identities.

## Consequences and boundaries

This path supports live independent-policy ordinary business sources with positive
remaining allowance, no open holds and no unfinished descendants. System, recovery,
plugin-controlled and protected-minting sources are refused. Expanded-tree transfer,
ordinary advisory review and separately funded terminal work need their own paths.

Review may already have consumed enough allowance to block revision. Cancellation
does not stop an OS process or undo an external effect. No new root grant, mutable
card status, claim reset or plugin-owned output qualification is introduced.
`task request-changes` publishes work; its returned full successor ID is continued
through ordinary `run --task`. It does not automatically invoke a provider.

Reports and verdicts remain policy-bound authorized assertions. Distinct actors
provide protocol attribution, not proof of distinct people or independent models.
This development behavior is not full Kanban or stable-release qualification.
