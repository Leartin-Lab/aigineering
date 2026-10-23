# Retain input roles and observations across tool continuations

Status: Implemented development increment
Date: 2026-10-23

New ordinary tool continuations retain their parent's declared input roles and
unchanged sensitive-input policy. Exact v6 bindings therefore keep selecting the
original source Assets through normal disclosure. Tool observations remain
method context, not additional input roles or business outputs.

Before publishing a new continuation, completion freezes the ordered, deduplicated
union of the exact parent's existing method context and the completed source's
outputs in its existing context-prepared trace. The scheduled trace uses the
same selection. An interrupted publication reuses the already prepared IDs and
rejects inconsistent or missing prepared context rather than refreshing it.
No Store-wide ancestor scan or arbitrary same-name observation lookup occurs.

Exact history recognition accepts either the current input-preserving Contract
or the legacy empty-input construction, retaining all original Candidate,
receipt, head/member, funding and materialization proofs. A matched legacy child
is returned unchanged; recognition cannot publish replacement work or add
context to its original selection. Multiple distinct publications remain an
integrity conflict. Historical already-published empty-input continuations are
not retroactively repaired; use explicitly authorized new work when they cannot
fulfil their obligation. Stop older Workers before relying on the new behavior.

The CLI research fixture exercises source input retention, two chained tools,
both complete observations, independent acceptance, recovery and export. Unit
and historical replay coverage also check input-policy preservation and exact
recognition of a previously committed empty-input child after reopen.
