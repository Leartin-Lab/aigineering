# ADR-015: Policy-bound independent output acceptance

Status: Accepted
Date: 2026-07-19
Related: ADR-002, ADR-011, ADR-013, ADR-014

## Context

An attestation over an exact output Asset is incomplete if it can be replayed
under a different rubric or acceptance policy. Concurrent attestations must
also never select two different Assets for one Contract output slot.

## Decision

An independent `acceptance_policy` has a non-empty `policy_version` and may
declare sorted, unique rubric and evidence Asset IDs. Its content-addressed
`policy_id` binds the complete immutable policy.

`asset.attest` carries the exact policy ID/version and the policy's exact
rubric/evidence IDs. Projection rejects missing, changed, unknown, or
non-committed context. Accepted attestation and qualification remain separate
facts.

The selected Asset may be produced by the Contract itself or by a Contract
whose immutable parent chain reaches the accepted Contract. This allows
ordinary planning and tool descendants to discharge a parent obligation while
rejecting unrelated Assets that merely reuse the output name.

The Store transaction enforces one selected immutable Asset per
`(contract_id, output_name)`. Repeated attestations for that same Asset
converge; an attempt to qualify a different Asset becomes a durable Candidate
rejection.

## Consequences

- a verifier cannot silently change the question it claims to have checked;
- restart and active-active replicas reconstruct the same qualified output;
- replacing an independently accepted output requires an explicit future
  replacement policy and a new attestation;
- v0.5 still requires one independent attestation and does not implement a
  quorum or verifier market.

## Evidence

- `tests/architecture/test_independent_acceptance.py`
- `tests/test_cli_acceptance.py`
- `conformance/v0.5.0/protocol-vectors.json`
