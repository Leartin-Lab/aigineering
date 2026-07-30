# ADR-017: Signed definition/content graph

Status: Accepted
Date: 2026-07-31
Related: ADR-002, ADR-007, ADR-014, ADR-016

## Context

An Asset's content bytes, semantic definition, source, and issuing authority
have different identities. v0.5 combines name with content identity and uses
name alone for definition identity. That cannot express the many-to-many
relationship between independently authorized definitions and reusable
content.

Embedding similarity can discover possible equivalence, but model output is
neither deterministic identity nor authority.

## Decision

Aigineering will represent:

- normalized content as a content-addressed object independent of name;
- a definition as a signed canonical statement whose accepted identity binds
  its statement, source semantics, signing key, and signature;
- the association between them as a separately signed, typed assertion.

One definition may link to many content objects and one content object may link
to many definitions. No link overwrites another.

Semantic matchers may publish relation Candidates containing model, version,
threshold, score, and evidence. The normal commitment boundary validates and
authorizes them. Similarity alone cannot merge identities, transfer authority,
or affect replay.

Contract construction resolves label syntax to exact committed references.
Labels remain audit metadata after commitment and are not a dynamic execution
control.

## Consequences

- content deduplication no longer collapses definition or signer authority;
- definition reuse becomes explicit and auditable;
- signatures are verified without circularly signing their own identifier;
- legacy Asset IDs require a versioned migration and compatibility projection;
- graph and semantic search can live in disposable query projections;
- protocol fixtures must define exact canonical bytes for all three objects.

## Acceptance

Accepted in v0.5.2 after schema migration, canonical vectors, legacy
reconstruction, exact-label replay, signature rejection, many-to-many
conformance, real Redis rebuild, and release-artifact verification passed.
