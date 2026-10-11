# ADR-035: Continue an expanded obligation through one ordinary successor

Status: Accepted
Date: 2026-10-04
Related: ADR-030, ADR-032, ADR-034

## Context

Leaf handoff cannot transfer a task after ordinary planning or tool continuation
has created descendants. Copying that execution tree would change immutable
ancestry, output ownership and acceptance. The original root obligation is the
scope to continue; intermediate execution topology remains history.

## Decision

The existing single-effect signed `task.handoff` operation cancels its business
source and selected live descendants, fences their claims, and declares one
ordinary successor atomically. It reuses canonical lifecycle, declaration and
allowance owners. The successor can plan or continue through ordinary runtime
work; handoff adds no scheduler, mutable card status or group/join semantics.
Its budget comes from the source's available balance plus validated unused donor
returns. Source balance zero is supported when eligible descendants hold funds.

Traversal continues through terminal intermediaries without rewriting them.
The shared completion predicate preserves branches serving a live independent
ancestor outside the transferred root, including all descendants and budgets.
A verifier serving only the cancelled source is closed. New successor output
still needs its own qualification. Open holds on transferred or completion-repair
nodes block with exact Contract/hold IDs; external preserved branches and existing
terminal intermediaries retain their own history.

A historical descendant with nonempty, canonically satisfied outputs but no typed
terminal receives an explicit `complete` terminal as an authorized handoff
consequence. Exact satisfying Assets and applicable qualification facts are
linked. Its claim is fenced and its own residual extinguished, not returned.
Traversal still closes unfinished grandchildren. Empty output sets are not proof
of completion. An untyped historical failed, cancelled or unreachable trace is
an observable history-conflict blocker. Read-only audit and rebuild never repair
these facts.

Expanded successors freeze the source's current legal input roles and disclosure
with v6 exact Asset bindings. They inherit execution, delegation, trust/redaction,
label, tool and acceptance scope. Child results, observations and authorities are
not copied into successor context. Already satisfied source slots and exact IDs
remain link metadata; the explicit operator reason remains Worker context.
An unresolved declared input role blocks expanded handoff before any transfer;
it cannot be frozen empty and later satisfied by an unrelated same-name Asset.

The link freezes selected cancellation, repair, preserved-branch and return
identities, output evidence, source balance, resolved budget and optional new hold.
History reads these committed facts. Nullable-budget replay restores the same
committed projection before inspecting later state and creates no second transfer.
Old leaf handoff identities, payloads and operator key remain unchanged.

## Consequences and boundaries

Writer-lock reprojection checks the tree, holds, output evidence, allowance and
effective keys before commitment. Changed decisions are durable rejection rather
than partial cancellation or replacement with a different batch. Cancellation
fences durable submissions; it does not stop OS processes or reverse external
side effects. Source eligibility still excludes terminal, system/recovery,
plugin-controlled and protected-minting tasks. Descendants may be plugin/tool
work without transferring those authorities to the successor.

Expanded handoff and its scoped historical completion repair do not establish
complete Kanban feature parity or stable-release qualification. Other historical
conflicts and the remaining release gates retain their own observable obligations.
