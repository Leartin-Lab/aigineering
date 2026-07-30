# Change 001: Candidate-native fact commitment

Status: Implemented and verified
Target: v0.5.0
Public decision: `docs/adr/ADR-011-candidate-native-plugin-runtime.md`

## Problem

Earlier runtime surfaces could create equivalent facts through different
control-plane, Worker, and feature-specific paths. That made authority, trace,
transaction, and reconstruction behavior depend on the caller.

## Resulting design

- Genesis establishes one signed fact domain and its root actor keys.
- Human, script, LLM, Plugin, and Engine-backed actors publish canonical signed
  Candidates containing typed effects.
- One commitment reducer validates signature, capability, references,
  authority, allowance, acceptance, and atomic groups.
- Rejected Candidates remain durable evidence; rejection never falls back to a
  direct write.
- Contracts are immutable task declarations. Eligibility, claims, blockers,
  outputs, terminal outcome, and allowance are projections of durable facts.
- Planning, replanning, retry, fail, recovery, continuation, tool use, and
  verification publish or complete ordinary tasks through Store-free Plugins.
- SQLite commits Candidate decisions, facts, trace, idempotency, claims,
  terminal consequences, and allowance consequences transactionally.
- Materialized tables can be deleted and rebuilt from RuntimeRecords with the
  same semantic digest.

## Public compatibility

v0.5.0 reads historical SQLite schemas through explicit migrations and accepts
documented older input aliases where they enter the same current validation
path. Compatibility does not provide a second fact-ingress, authority,
terminal, or replay implementation.

The removed direct-ingress and process-owned lifecycle modules are not shipped
in the wheel or sdist.

## Required verification

- canonical signed Candidate and Genesis vectors;
- fail-closed actor, key, domain, and capability validation;
- pure effect projection and one commitment coordinator;
- visible acceptance and rejection receipts;
- claim/package/epoch fencing and idempotency;
- causal allowance containment under concurrency;
- independent exact-Asset output attestation;
- Memory/SQLite conformance;
- crash rollback and materialization rebuild;
- release artifact inspection.

## Closure

The change is complete in v0.5.0. Current runtime truth is documented in
`DESIGN.md`; release evidence is documented in
`reports/050-post-review-boundary-hardening-2026-07-19.md`.
