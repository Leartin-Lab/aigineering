# Change 007: Ordinary output and Asset graph convergence

Status: Implemented and verified on dev; release pending
Target: v0.5.3
Decision: `docs/adr/ADR-017-signed-definition-content-graph.md`

## Problem

The signed definition/content/assertion graph is available through explicit
typed effects, but ordinary Worker `/exec` output still publishes a legacy
Asset whose identity is only its name and content. Two authorized publishers
can therefore produce the same Asset ID with different provenance, causing a
late immutable-record conflict instead of representing two assertions over
reusable content.

## Intended change

- authenticated Worker output publishes normalized content, a signed output
  definition, and a signed definition-content assertion as one Candidate;
- accepted assertions project the compatibility Asset needed by activation,
  disclosure, completion, CLI, and historical views;
- compatibility Asset identity follows the assertion, while `content_hash`
  and `definition_hash` refer to their independent graph identities;
- legacy `asset.propose` remains accepted, but its materialization identity
  also binds Candidate provenance so equal bytes from different publishers do
  not collide;
- replay and reconstruction derive identical compatibility views without
  consulting Redis or a semantic matcher.

## Non-goals

- changing graph endpoint or assertion schemas;
- merging semantically similar definitions;
- making Redis or embeddings authoritative;
- rewriting historical Asset IDs;
- requiring the runtime to hold a Worker's private key.

## Verification

- equal content from two signed definitions shares one content object and has
  distinct definitions, assertions, and compatibility Assets;
- one definition may link to multiple contents without overwriting history;
- ordinary claim-bound output still satisfies and completes its Contract;
- graph signature, claim containment, and output authority failures reject the
  whole Candidate;
- SQLite rebuild, Redis rebuild, replay, CLI/API views, and full release gates
  remain stable.

## Exit criteria

Ordinary production Worker output uses the accepted many-to-many graph, legacy
publication no longer collides across provenance, and every existing task
consumer observes the same authorized output semantics.

## Implementation evidence

- WorkerHost builds one signer-authenticated graph batch for `/exec`; graph
  endpoint and edge validation remains in pure projection;
- accepted assertions deterministically materialize sealed compatibility
  Assets and causally rooted `asset.committed` facts;
- legacy proposal identity binds Candidate signature and provenance without
  rewriting historical rows;
- equal Worker output content produced one content object, two signed
  definitions, two assertions, and two compatibility Assets;
- partial claim-bound graph batches rejected without committing endpoints;
- Memory and SQLite compatibility projection, verification, reconstruction,
  Redis-backed reads, Worker protocols, CLI/API, and independent acceptance
  tests passed;
- full deterministic suite passed with 1124 tests and 3 skips; Ruff check and
  format passed; wheel and sdist built successfully.
