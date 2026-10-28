# ADR-040: Declare direct facts separately from reserved ownership

Status: Accepted
Date: 2026-10-05

## Context

Trusted host callbacks project injected effects. The existing handler field
`emitted_fact_types` carries assignment admission metadata; compound handoff
and revision handlers also emit legitimate facts outside that field. Treating
it as a complete output allowlist would change existing semantics. Multiple
handlers legitimately emit shared declaration, terminal and hold facts.

Future task controls need a complete direct-output declaration and, where
appropriate, an exclusive fact owner. These declarations must be frozen with
the handler mapping and checked through the existing commitment boundary.

## Decision

Add two keyword-only fields to `EffectHandler`:

- `direct_output_fact_types`: an optional complete set of record types returned
  directly by the projector. `None` preserves trusted compatibility behavior;
  an explicit empty set permits no direct records.
- `reserved_fact_types`: a subset of an explicit direct-output set, with exactly
  one owning effect in the frozen injected mapping. A different effect cannot
  return such a type, even through a compatibility-mode handler.

Preserve `emitted_fact_types` and `assignment_admission` semantics. Copy and
freeze descriptor sets, reject duplicate exclusive owners during composition,
and validate fresh projector records before generic co-effects are generated.
Validate the typed projection and its tuple of canonical RuntimeRecords before
reading member fields, so malformed callback returns produce durable rejection.
Shared fact types remain nonexclusive. All in-tree operational injected
registries opt into complete direct-output declarations. External old typed
descriptors and tuple registrations retain explicit trusted compatibility.

The existing per-record effect owners and pure projection fingerprint remain
the transport to commitment. The SQLite writer reruns the same decision using
the frozen mapping and current facts and keys. No Store ledger, table, schema
floor, feature imports or second scheduler is added.

## Exact replay

Existing single-effect compound replay helpers return the original complete
committed sequence, including generic co-effects. Removing those records would
change the committed head; adding them all to a fresh direct-output declaration
would incorrectly broaden the callback's authority.

A Core-owned exception permits only an exact single-effect replay. It verifies
the currently authenticated Candidate's unique receipt and committed head,
receipt identity and actor/key/effect types, valid original records, unique
ordered member IDs and exact head causal parents. The returned sequence must
equal the full committed membership with no new, missing, duplicate or foreign
record. Only the generic `allowance.granted`, `allowance.reserved` and
`asset.committed` co-effects may be outside the strict direct set. Unlisted
feature facts remain rejected. Reserved ownership is checked on every returned
record, including those historical co-effects. Only declared direct types
receive the effect-owner attribution; generic co-effects do not become
plugin-owned.

No projector-provided replay flag grants the exception. Multi-effect callbacks
retain normal per-effect direct checks; a full-head exception cannot infer
ownership of shared types across several effects. Legacy compatibility remains
explicit rather than silently reclassifying such records. Writer re-projection
repeats the same classification before mutation.

## Consequences and alternatives

This constrains declared host callbacks without granting a capability. Normal
Candidate authentication and effect authority still apply. It does not sandbox
Python callbacks or make arbitrary legacy extensions safe for untrusted code.
Raw trusted Store helpers are not an operational publication guarantee.

Reinterpreting the old field would break compound effects and external typed
handlers. Reserving every fact type exclusively would forbid legitimate shared
records. A mutable global registry or a second Store ledger would duplicate
existing ownership and writer validation. Silently omitting replay co-effects
would change historical decisions. Complete declarations, selective exclusivity
and verified original replay preserve those boundaries separately.

Implementation and acceptance evidence remain separate from this decision.
Task priority and stable-release qualification require their own verification.
