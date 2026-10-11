# 045: Bounded successors after exact changes decisions

## Problem

An independent review's changes verdict preserves a rejected output and submitted
claim, but creates no new executable revision. Resetting the claim or assigning a
fresh root budget would lose lineage and widen authority.

## Resulting design

A sole-effect signed `task.revise` Candidate binds the exact committed decision,
request, rejected attestation, target, report and frozen policy. One shared pure
reference validator serves application and plugin. Canonical cancellation and
publication atomically fence the old attempt, reserve current allowance and declare
a successor carrying every unqualified output slot. Qualified slots remain exact
history metadata. Shared successor helpers also serve existing handoff.

The child retains execution and acceptance scope, input roles and trust checks.
It freezes original input selections and legacy label references, adding only the
exact rejected target and report as context. Those references cannot become new
policy evidence. Fresh output requires fresh qualification; predecessor cancellation
is immutable even after successor completion.

The CLI provides `task request-changes DECISION_ID --reason TEXT [--budget N]` and
`task revisions SOURCE_ID`. Omitted budget remains null in signed input and resolves
once. Exact replay restores the original successor before mutable checks. Redis
relay failure preserves committed facts and reports synchronization pending.
See [ADR-033](../docs/adr/ADR-033-review-linked-task-revisions.md).

## Public compatibility

Revision supports live independent-policy ordinary business sources without open
holds or unfinished descendants and with sufficient allowance. Unsupported origins,
protected minting and existing terminals are refused. The command does not execute
a provider. Continue its returned full successor ID with `run --task`.
No claim reset, new root grant or retrospective acceptance gate is added.

## Required verification

Check multi-output transfer and retained qualification, exact provenance failures,
old-claim fencing, fresh successor qualification, allowance/replay/rollback, forced
writer contention, disclosure equality, installed CLI/Redis/reopen/rebuild/export,
signed cross-language vectors and a retained real changes-task continuation.

## Closure

Development evidence belongs in [report 078](../reports/078-review-linked-task-revisions-2026-10-04.md).
Expanded-tree handoff and the remaining Kanban/stable-release gates stay open.
