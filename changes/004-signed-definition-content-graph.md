# Change 004: Signed definition/content graph

Status: Proposed; implementation begins after Change 003 closes
Target: 0.5.2
Public decision: `docs/adr/ADR-017-signed-definition-content-graph.md`
Depends on: v0.5.1 disposable query projection

## Problem

In v0.5.0, `definition_hash` is derived from an Asset name and `content_hash`
is derived from both name and content. This makes identical bytes under two
definitions look like different content, and it does not make the authority
chain of a definition part of its identity.

A definition and content are not one-to-one:

- one signed definition may admit several content versions;
- identical content may satisfy several independently signed definitions;
- two similar definitions may be related without being identical or sharing
  authority.

## Intended change

Introduce three explicit immutable identities:

1. **Content identity** hashes normalized content only.
2. **Signed definition identity** hashes the canonical definition statement,
   its source semantics, signing key identity, and signature. The signature is
   verified over the unsigned canonical statement before the identity is
   accepted.
3. **Definition-content assertion** is a signed relation between one definition
   and one content object. The relation, not either endpoint, carries
   provenance and relation-specific evidence.

The resulting graph is many-to-many. Existing Asset views become projections
over accepted definition-content assertions.

Semantic similarity is advisory. An embedding Worker or Plugin may propose a
typed relation Candidate with model/version/evidence metadata. Projection and
policy decide whether that relation becomes a fact. A transient similarity
score never rewrites an identity, widens authority, or changes replay.

Labels remain construction-time syntax. Before commitment, label expansion
resolves to exact Asset or definition references bound into the Contract.
After commitment, labels are audit metadata and are not re-evaluated to alter
disclosure or execution during replay.

## Migration

- add versioned content, signed-definition, and assertion facts;
- migrate legacy Assets without changing their historical IDs;
- create explicit legacy assertions that retain original signer, origin,
  lineage, and hash fields;
- provide read projections compatible with current Asset CLI output;
- version canonical protocol fixtures and reject ambiguous mixed-version
  writes;
- rebuild Redis projection only from migrated SQLite facts.

## Non-goals

- treating embedding distance as cryptographic identity;
- silently merging definitions from different signers;
- making names globally unique;
- letting Redis own graph truth;
- allowing label lookup to change a committed Contract on replay.

## Exit criteria

- identical normalized content under different definitions has one content ID;
- one definition links to multiple contents and one content links to multiple
  definitions without overwriting history;
- signer or source changes produce a distinct signed-definition identity;
- invalid signatures and unsigned semantic links fail closed;
- legacy databases migrate and reconstruct to an equal semantic digest;
- label-backed Contracts replay from exact committed references after the label
  catalog changes;
- language-neutral canonical vectors, full tests, build, and artifact gates
  pass.
