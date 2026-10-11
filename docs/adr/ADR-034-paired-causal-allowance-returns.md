# ADR-034: Return unused causal allowance along its original funding path

Status: Accepted
Date: 2026-10-04
Related: ADR-013, ADR-030

## Context

An expanded task can have no available allowance because its children hold the
remaining grant. Transferring only the parent's balance cannot continue that
original obligation. Adding full child grants would count descendant reservations
more than once. Returning allowance must conserve the original root grant.

## Decision

A generic `allowance.returned` consequence credits `source_contract_id` and debits
`from_contract_id` by the same positive integer amount. The recipient must be a
strict funding ancestor. Exact committed grant/reservation pairs, their Candidate
heads and receipts prove the complete path; Contract ancestry alone is insufficient.
The return binds its Candidate, receipt, donor cancellation and funding records.
The donor must be live before the batch and cancelled by it. Previously terminal
allowance cannot be reclaimed. A live grandchild can return directly through a
terminal intermediary without crediting or reopening that intermediary.

Pure projection validates returns before ordinary child reservation uses their
credits. Any new return requires cancellation and Contract-publication authority.
The writer independently checks current balances and actual Candidate receipt/head
closure in the same transaction. Terminal extinction uses the residual after
returns and reservations. Exact committed replay creates no new credit.

Feature authorization remains in the trusted plugin. The sole current producer
is signed task handoff; it validates the single effect and attributed link, then
uses this generic conservation owner. Writer-lock reprojection rechecks its full
immutable decision. No standalone return effect, CLI command, extra capability,
mutable account or new root grant is introduced. Direct Store record append cannot
publish allowance grants, reservations, returns or extinguishments. New grants
must match the exact declared Contract budget and deterministic funding pair;
their declaration, receipt and committed head must agree. A future return
producer needs its own reviewed authorization rule.

Low-level Store transaction primitives and implementation code remain trusted.
These structural checks do not cryptographically authenticate arbitrary Python
calls or a coordinated rewrite of local records. Supported external publication
still requires authenticated Candidate admission before the writer transaction.

## Compatibility and consequences

SQLite schema19 is the normal reader capability floor. A schema18 reader only
credits returns and must refuse a schema19 Store, even before a return exists.
Normal migration preserves valid existing rows and only advances the version. Schema18
has no supported return producer; migration refuses any preexisting return row
rather than bless a raw import or reinterpret credit-only history. The failed
migration preserves its contents and original schema version. Migration also
checks historical grants and reservations against canonical declared budgets,
funding pairs and committed declaration provenance; unsupported extra grants
are an explicit upgrade failure rather than transferable allowance. Legitimate schema19
returns reopen normally without migration.

Use `aig domain upgrade --json` for the existing backup-before-migration flow.
Ordinary writable opens also migrate; read-only older-schema opens require an
upgrade first. Stop old Workers before upgrading: already-open connections and
raw imports are outside the compatibility gate. No table, column, RuntimeRecord
schema or Contract identity version changes. Historical v3-v6 identities stay
unchanged. Causal allowance remains a bound on work expansion, not token billing
or provider cost.

Rejecting every expanded task leaves the product gap. Cloning the old tree changes
ancestry and acceptance. Issuing another root grant amplifies the budget. Paired
returns allow the same obligation to continue through ordinary contained work.
