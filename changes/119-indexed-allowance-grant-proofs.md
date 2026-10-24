# Change 119: Reuse indexed proofs for allowance grants

## Problem

One `validate_allowance_grants` call validates a batch of child grants against
the same Candidate declaration, committed head and receipt. Repeating full
record scans, head membership scans and envelope/hash validation for each grant
makes this shared proof work grow quadratically with batch size.

## Resulting design

The validator builds a call-scoped relation index for committed-head members,
Candidate/Contract declarations, new reservations and existing grants. It
reuses a successful committed-head and receipt proof, including their complete
record validation, within that call. Per-head membership sets support the
existing membership checks without repeatedly traversing the same member and
causal-parent sequences. Duplicate matches remain visible to the existing
ambiguity checks. The index and proof memo are ephemeral; they add no persistent
cache, table, authority or writer path.

Opaque malformed historical containers retain the original scan predicates
and downstream validation behavior. The existing funding, declaration,
receipt, head-closure, writer transaction, authentication and rebuild semantics
remain in force.

## Verification and scope

Signed one-batch tests at P=8, 16, 32 and 64 checked complete head/receipt
validation, actual member/parent sequence access, duplicate members and
reservations, and opaque legacy containers. The independent old/new semantic
differential checks also passed. For the shared head and receipt, old full
envelope/hash validation counts grew with batch size; the indexed validator
validated each once:

| P grants | Head validations before → after | Receipt validations before → after | Member + parent accesses before → after |
| --- | ---: | ---: | ---: |
| 8 | 40 → 1 | 88 → 1 | 15,504 → 342 |
| 16 | 80 → 1 | 176 → 1 | 61,344 → 678 |
| 32 | 160 → 1 | 352 → 1 | 244,032 → 1,350 |
| 64 | 320 → 1 | 704 → 1 | 973,440 → 2,694 |

For well-formed inputs and fixed record payload size, this local proof path
reduces repeated work to O(H + M + P), where H is the supplied record history,
M is the total committed-head member count, and P is the grant batch size.
Complete envelope validation also processes payload bytes; for variable-size
records, validation bytes V are an additional cost. This change still indexes
the supplied history on each publication and does not remove same-parent
allowance-history rescans across separate publications. It closes neither
those costs nor the overall sustained-workspace scaling goal. Full integrated
and release gates remain separate from this local result.
