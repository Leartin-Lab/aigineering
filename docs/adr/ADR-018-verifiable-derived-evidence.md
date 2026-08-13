# ADR-018: Verifiable derived evidence

Status: Accepted
Date: 2026-08-13
Related: ADR-002, ADR-007, ADR-015, ADR-017

## Context

A signed replacement relation proves who asserted a relation. It does not prove
that caller-supplied content is an exact slice of a committed source. Policies
that accept a claim type must not turn the claim type string into semantic
truth.

## Decision

An exact slice relation binds the source Asset, replacement Asset, lineage,
range specification, and derivation algorithm version. `slice-v1` supports
line, character, and UTF-8 byte ranges. Construction and verification
recompute replacement content from the committed source; byte ranges that split
a UTF-8 character fail closed.

A disclosure policy that declares accepted claim types requires a valid
incoming relation for the exact Asset. Signed but invalid relation Candidates
remain auditable assertions and do not satisfy the policy.

## Consequences

- replay is independent of a changed name or label catalog;
- arbitrary caller-supplied slice content cannot gain trust from a relation;
- derivation evolution requires a new explicit algorithm version;
- semantic similarity remains advisory and is not an exact derivation proof.

## Evidence

- `tests/test_verified_derivations.py`
- `tests/test_batch_verify.py`
- `tests/test_asset_graph_protocol.py`
