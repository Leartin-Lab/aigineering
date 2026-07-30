# v0.5.2 signed definition/content graph acceptance

Baseline: v0.5.1 commit `ac697bd`

Scope: signed many-to-many definition/content facts, exact label-context
binding, and reconstructable SQLite/Redis read views

## Boundary result

- Content identity hashes NFC-normalized content only.
- Definition identity binds canonical source semantics, domain, actor key, and
  a verified Ed25519 signature.
- Each definition-content association is a separately signed assertion.
- All graph writes are typed effects inside an actor-signed Candidate. Missing
  endpoints, wrong domains, unauthorized keys, and invalid signatures are
  durable rejections.
- Semantic matching is an advisory adapter that can only publish an ordinary
  relation Candidate.
- v4 Contracts bind label-selected context to exact Asset IDs before
  commitment; recursive plan, replan, retry, continuation, recovery,
  delegation, and Engine-as-Worker paths preserve the binding.

## Reconstruction result

- SQLite schema v15 materializes contents, definitions, and assertions from
  RuntimeRecords.
- Existing Asset IDs are preserved. Explicit schema-0 migration records retain
  legacy identity and provenance without claiming a v1 actor signature.
- Deleting materialized graph rows and reopening the Store reproduces the same
  runtime materialization digest.
- Redis query schema v2 is disposable. Flush, stale catch-up, graph update, and
  generation replacement rebuild compatibility Asset and graph views from
  SQLite.

## Protocol and interface result

- `conformance/v0.5.2/asset-graph-vectors.json` fixes canonical content,
  definition-signing, definition identity, assertion-signing, and assertion
  identity bytes.
- `aig graph contents|definitions|assertions` and matching HTTP GET endpoints
  are read-only projections.
- Existing `asset.propose` publication materializes a legacy graph record in
  the same accepted Candidate batch.

## Verification

The release gate covered:

- protocol, signature, authority, atomic batch, many-to-many, and migration
  regression tests;
- exact label replay and every recursive task-construction path;
- real Redis flush, rebuild, stale catch-up, outage fallback, and graph views;
- canonical conformance-vector consumption;
- Ruff lint and format;
- the full deterministic suite;
- wheel and sdist build, metadata validation, artifact-content inspection, and
  installed-wheel CLI/database smoke tests.

Release-candidate local result:

```text
1116 passed
ruff check: passed
ruff format --check: passed
build: aigineering-0.5.2.tar.gz and aigineering-0.5.2-py3-none-any.whl
twine check: passed
```

The suite ran with a real Redis instance configured for both normal query reads
and Redis integration cases. Its only warning was an upstream FastAPI
TestClient dependency deprecation.

The sdist contains the v0.5.2 conformance vector, public design, ADR, change,
Skill, and this evidence report, and excludes tests and private workspaces. A
clean wheel installation reported version 0.5.2, initialized and reopened a
domain, published an Asset, read all three graph views, rebuilt Redis query
schema v2, and read the same content through that projection.

## Scope limit

v0.5.2 does not make embeddings cryptographic identity, infer authority from
similarity, add a distributed Store, or turn Redis into a commitment database.
It remains the documented single-machine reference runtime.
