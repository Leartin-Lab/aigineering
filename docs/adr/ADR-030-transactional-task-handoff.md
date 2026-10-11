# ADR-030: Transfer unfinished work through a signed successor

Status: Accepted
Date: 2026-10-04

## Context

An operator must be able to hand off an active attempt without resetting its
claim or silently granting another full root allowance. An immutable predecessor
and an explicit successor preserve the history and fencing of each attempt.

## Decision

The handoff plugin owns a single-effect `task.handoff` Candidate. Its authenticated
actor also needs cancellation and Contract-publication capabilities, and a hold
capability when requesting a held successor. It reuses canonical cancellation
and declaration projection. The successor reserves bounded remaining allowance
from its predecessor; only untransferred residual allowance is extinguished.
SQLite commits cancellation, claim release, successor, allowance and an attributed
handoff link atomically. Late submission under the old claim is fenced.

The signed request carries a nullable budget. A null request resolves against
the first accepted projection; exact replay recognizes the committed link before
rechecking state and returns its original successor and budget. The application
does not convert null into a mutable budget before signing.

The successor inherits execution and disclosure scope and the frozen acceptance
policy. It declares only unfinished output slots. Already satisfied slot names
and exact Asset IDs remain auditable link metadata; their content is not
automatically added to disclosure. Predecessor qualification cannot stand in for
qualification of an independent-policy successor.

The operator's explicit reason is appended to successor description and becomes
Worker context. Retained result contents are not automatically disclosed.
Ordinary tasks preserve the existing global same-name business-output rule;
retained IDs do not prove predecessor production. Independent acceptance uses
the existing exact Contract/Asset qualification owner.

Trusted injected effect handlers remain pure. For accepted Candidates handled
by an injected registry, commitment reruns the same pure decision inside the
SQLite writer transaction with current facts and effective actor keys. It
compares immutable projection identities with the admitted decision and rejects
changes before writes. Built-in behavior is not replaced; the core does not
import the handoff plugin. This check prevents a pre-transaction view from
silently authorizing work after relevant state has changed.

A key revoked between projection and commitment produces an authentication
rejection without reusing an invalid authenticated receipt. Other snapshot
conflicts retain the ordinary rejection receipt and its causal link.

## Consequences and boundaries

An active claim may be closed by authorized handoff. Cancellation fences future
durable submissions; it does not stop an operating-system process or reverse
external actions already performed. The current slice refuses unfinished
descendants, open predecessor holds and plugin-controlled or protected-minting
tasks. It does not yet transfer an expanded task tree. These refusals must be
visible rather than leave live descendants behind a cancelled parent.

[ADR-035](ADR-035-expanded-root-handoff.md) extends this original leaf-only
decision to expanded business obligations. It closes selected descendants and
continues through one ordinary successor rather than copying the old tree.
The original leaf projection and replay identities remain unchanged.

Polling a late rejected submission for an already cancelled, complete or
unreachable source records `recovery_unavailable` with `terminal_preserved`;
it does not schedule more work or add a conflicting failure trace. Failed-task
recovery remains available. The separate window between recovery precheck and
concurrent source cancellation still requires transactional qualification.

Current and historical task projections read terminal facts directly. A terminal
published by an authenticated Candidate cannot depend on a separate terminal
trace to become visible.

Resetting the old claim loses attempt provenance. Reusing retry as handoff can
grant another root allowance or reserve from the wrong parent. Display-only
state checks permit races. The chosen operation uses ordinary tasks and the
existing commitment owner, with explicit links and transactional revalidation.
