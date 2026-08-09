# Aigineering Design

Status: implemented truth for v0.5.2

This document describes the code shipped in the v0.5.2 reference release.
Future designs do not belong here until their implementation, tests, migration,
and release evidence are complete.

## Scope

Aigineering is a single-machine zero-trust runtime for stateless AI Workers.
SQLite is the authoritative Store. Runtime progress is reconstructed from
durable facts rather than from an Engine process, conversation, or private task
state.

The kernel is responsible for:

- signed Candidate admission;
- effect projection and authority;
- causal allowance;
- claim fencing and idempotency;
- atomic fact and trace commitment;
- deterministic runtime projection.

Provider prompts, tools, planning strategies, and user interfaces live outside
the commitment kernel.

## Implemented runtime path

Every supported publication surface follows the same path:

```text
actor intent
→ canonical typed Candidate
→ actor signature verification
→ capability and reference validation
→ pure effect projection
→ authority and allowance validation
→ one SQLite commitment transaction
→ immutable runtime facts
→ derived query views
```

Genesis bootstrap is the only operation that precedes Candidate admission. A
rejected Candidate never falls back to a direct write.

## Fact domain and actors

Each Store has one immutable Genesis manifest containing the domain identity,
root public keys, and policy identity. Humans, scripts, LLM Workers, Plugins,
and Engine-backed Workers are actors identified by keys and capabilities rather
than privileged Python call paths.

Ed25519 signatures authenticate Candidate actors. Deterministic Asset seals
remain integrity checks and are not accepted as actor identity.

Key rotation, actor authorization, capability grants, and Worker registration
are represented by signed effects and durable facts.

## Protocol values

Protocol values are canonical, deeply immutable, and content addressed.
Signed JSON uses a language-neutral canonical subset:

- string object keys;
- Unicode NFC normalization;
- no floats, NaN, Infinity, bytes, sets, or unsafe integers;
- stable ordering and serialization.

Contract identity binds every field that changes execution authority, including
parent, inputs, outputs, activation, allowance declaration, tools, labels,
Worker requirements, origin, minting authority, sensitive-input policy, and
acceptance policy. Current v4 Contracts also bind the exact Asset IDs resolved
from label syntax at construction time. Replay and recursive task publication
reuse those IDs instead of consulting a changed label catalog.

The public conformance vectors in `conformance/v0.5.0/` cover canonical bytes,
Candidate identity and signature, Genesis, Contract identity, effects,
attestation, and allowance. The vectors in `conformance/v0.5.2/` cover content
identity, signed definitions, and signed definition-content assertions.

## Asset identity graph

Content identity, definition identity, and their association are separate:

- a content object hashes only NFC-normalized content;
- a signed definition binds its name, media type, description, source
  semantics, domain, actor key, and Ed25519 signature;
- a signed assertion links one definition to one content object and carries
  relation-specific evidence.

The association is many-to-many. The same content may satisfy independently
authorized definitions, and one definition may have multiple content versions.
Neither case overwrites history or transfers authority between signers.

The three graph fact types enter through ordinary typed Candidate effects.
Endpoint existence, domain binding, actor keys, and signatures are validated
before commitment. An authenticated WorkerHost translates `/exec` output into
one atomic content/definition/assertion batch using its own key; the runtime
never holds the private key. Accepted assertions deterministically materialize
the compatibility Asset and `asset.committed` fact used by activation,
completion, disclosure, and historical projection. Its identity follows the
assertion while its definition and content fields reference the independent
graph identities.

Legacy Assets retain their historical IDs and receive explicit schema-0
migration records without being presented as newly signed facts. New
compatibility `asset.propose` materializations bind their Candidate signature
and provenance so equal bytes from different assertions cannot collide.

Semantic matchers are advisory adapters. They may publish a typed, signed
relation Candidate with model, version, threshold, score, and evidence, but
similarity never changes an identity or bypasses commitment.

## Candidates and effects

A Candidate is an immutable actor-signed proposal containing typed effects.
The supported effect families include:

- actor and key facts;
- Worker registration and coordination;
- Contract declaration and cancellation;
- Asset proposal and relation;
- Plugin invocation;
- output attestation.

Signature verification proves who proposed the effects. It does not grant the
effects. The commitment reducer separately validates capabilities, references,
atomic groups, claim delegation, allowance, authority, and acceptance.

Accepted and rejected decisions are both durable. Rejection is a protocol
outcome, not an exception that may be omitted from replay.

## Contracts and task progress

A Contract is an immutable obligation:

```text
declared inputs
declared outputs
monotonic activation
causal allowance
tool and Worker constraints
authority and acceptance policy
```

Task status is a projection, not stored mutable state. The runtime derives:

- whether required facts exist;
- whether activation is true;
- whether a claim is active;
- remaining causal allowance;
- whether declared outputs are satisfied;
- terminal outcome and descendant risks.

`blocked` means the current facts do not enable execution. It is not an
Engine-owned waiting state. Malformed activation is an explicit defect, not a
predicate that remains false forever.

One immutable terminal fact exists per Contract. Retry and recovery publish new
Contracts; they do not reopen a closed attempt. Each causal module may derive
an outcome, but `core.lifecycle_facts` alone constructs the canonical terminal
RuntimeRecord and the Store alone arbitrates single assignment.

## Worker protocol

Workers pull work rather than receiving mutable task objects.

```text
eligible Contract
→ atomic claim with epoch and lease
→ frozen WorkerPackage
→ Worker invocation
→ signed Candidate bound to Contract/claim/package/epoch
→ atomic submission
```

One active claim is allowed per Contract. Renewal and submission prove
possession of the registered Worker key. Expired claims, invocation failures,
malformed Worker results, and Contract terminal facts close visibly. A terminal
fact releases an unrelated active claim in the same transaction and records an
immutable transition that rebuilds to the same claim view. A stale Candidate
cannot publish after terminal closure; exact replay of an already committed
Candidate remains idempotent. Recovery is published only when its Candidate is
accepted.

The SQLite submission transaction rechecks the claim fence and commits the
Candidate, projection, accepted facts, trace, idempotency record, attempt
outcome, allowance consequences, and claim transition together.

## Disclosure

A Worker receives a frozen package containing the exact disclosed Asset IDs and
content views for that attempt. Disclosure is restricted to declared inputs and
promptable label-referenced Assets, then filtered by redaction and sensitive
input policy.

Behavior Assets require configured-or-higher trust before they may become
instructions. Reserved runtime Assets and non-promptable data are not exposed
by ordinary disclosure.

The package identity binds its Contract, disclosed Assets, claim, epoch,
Worker profile, tools, and remaining allowance. A submitted Candidate therefore
cannot claim a different context than the one issued by the runtime.

## Projection and authority

Pure projectors convert individual effects into immutable proposed facts. The
batch projector owns:

- atomic groups;
- capability checks;
- claim containment;
- delegated authority;
- allowance reservations;
- acceptance constraints.

Projection does not mutate the Store. The commitment coordinator is the only
owner of the write transaction.

For ordinary Worker output, declared output names are the exclusive allow-list.
Reserved runtime namespaces require exact inherited minting authority. Tools
and observations cannot satisfy business outputs merely because their names
appear in the Store.

## Plugins and recursive work

Plugins are Store-free proposal functions. They receive a frozen request and
return ordinary Candidate effects. They do not receive an Engine or mutation
handle.

Planning and replanning publish three ordinary tasks in one atomic group:

```text
draft
→ dependency analysis
→ compile
→ ordinary child Contracts
```

Each stage is independently claimable and testable. Compile enforces:

- non-empty executable descriptions and outputs;
- monotonic activation syntax;
- input reachability from disclosed facts or accepted producers;
- complete parent-output recommitment;
- tool, Worker, authority, and allowance containment.

A final reachability closure is computed from accepted producers only. A
rejected producer, self-dependency, or ungrounded cycle cannot leave an admitted
task permanently disabled.

Replanning uses the same task protocol recursively. Tool use, fail, retry,
continuation, recovery, and verification also publish or complete ordinary
facts through registered Plugins.

## Causal allowance

Allowance is lineage authority, not a Worker wallet or mutable counter.

- a root declaration creates an immutable grant;
- child publication reserves from its causal parent and grants the child;
- terminal projection extinguishes the unreserved remainder;
- exact Candidate replay cannot reserve twice.

Pure projection rejects an oversized batch. SQLite repeats the balance check in
the commitment transaction so concurrent publishers cannot overspend the same
grant.

Completion, recovery, continuation, task status, and audit traces read the same
allowance projection. There is no process-local budget owner. Historical
Contracts without allowance facts fall back to their declared budget only for
compatibility.

## Independent acceptance

An authorized output assertion is not automatically semantic truth. A Contract
may bind an independent acceptance policy.

For such a Contract, completion requires an attestation from a different actor
with all required verifier capabilities. The attestation binds:

- policy ID and version;
- Contract and declared output slot;
- exact produced Asset ID;
- committed rubric and evidence Assets.

Producer self-attestation, wrong-slot Assets, missing evidence, and replacement
of an already qualified slot fail closed.

## Persistence and reconstruction

RuntimeRecord is the append-only replay envelope. SQLite materializes Contracts,
Assets, trace entries, claims, Worker registrations, idempotency, replacement
claims, allowance, and terminal views for efficient access.

Materialized views are disposable. Rebuild:

1. reads immutable runtime records;
2. reconstructs materializations;
3. computes a semantic digest;
4. verifies it matches the pre-rebuild digest.

Historical SQLite schemas are migrated explicitly and tested. Missing causal
evidence, conflicting records, or unrecorded rows fail reconstruction rather
than being silently ignored.

## Concurrency and restart

Concurrency correctness belongs to Store transactions and uniqueness
constraints, not process-local locks.

Multiple same-machine Worker processes may use separate SQLite connections over
one WAL database. Claim epochs fence stale results. A replacement process can
reconstruct task progress and continue without restoring an Engine snapshot.

EngineWorker applies the same rule across nested fact domains. It receives an
outer claim context, executes against an isolated inner Store, persists bridge
operations, and exports only authorized output effects. A restarted
EngineWorker can reopen the inner Store and reuse committed work; an expired
outer claim cannot submit a late result.

## Query and interface surfaces

CLI and optional HTTP endpoints are adapters over the same Candidate and Worker
protocols. They do not own alternate mutation semantics.

SQLite remains the authoritative query fallback. When
`AIGINEERING_REDIS_URL` is configured, read-only Asset, Contract, capability,
task-status, and asset-graph views may use a Redis projection:

```text
SQLite RuntimeRecords
→ domain/schema-scoped Redis generation
→ monotonic authoritative revision watermark
→ read-only CLI/API view
```

A complete generation is published atomically. Later immutable Asset and
Contract records catch up idempotently, and the watermark advances
monotonically in the same Redis transaction. A current read compares the Redis
watermark with SQLite before using the projection. Missing, stale, partial,
graph-incompatible, or unavailable cache state rebuilds or falls back to
SQLite.

Redis is not a Store implementation. Commitment, authority, allowance,
acceptance, claims, fencing, idempotency, and terminal decisions do not import
or query the Redis adapter. Cache namespaces bind the Genesis domain and
projection schema, so independent Stores cannot share a generation.

The CLI provides domain initialization, task and Asset publication, Worker
execution, trace, audit, replay, recovery, and status views. The optional
FastAPI adapter creates one Store connection per request and closes it
deterministically.

The reference HTTP server is not a hostile-network deployment profile. TLS,
service authentication, rate limiting, and network policy remain external
responsibilities.

## Module ownership

The main responsibility boundaries are:

```text
protocol/     canonical wire values and signed effects
core/         commitment, projection, authority, Store, replay, trace
plugins/      Store-free task proposal and completion semantics
agent/        Worker adapters, prompts, provider and tool execution
cli/          local user and Worker protocol adapters
server/       optional HTTP transport
```

Feature-specific semantics do not belong in the commitment coordinator.
Store-specific transaction mechanics do not belong in Plugins or Workers.

## Known transition boundaries

The supported release retains only bounded compatibility needed to read
historical databases and accepted public input forms:

- schema migrations preserve older SQLite facts;
- historical trust-tier aliases normalize to current enum values;
- compatibility planning JSON enters the same containment compiler as staged
  planning;
- the deprecated `aig contract run` command fails with guidance to use
  `aig run --task`;
- deterministic Asset seals are integrity metadata, not actor signatures.

Compatibility paths may not create an alternate fact-ingress, authority,
terminal, or replay owner.

## Release limits

v0.5.2 is a stable local reference release, not:

- a cross-machine distributed Store;
- a consensus implementation;
- a public-network security profile;
- an external security audit;
- a guarantee that authorized model content is true;
- a generic workflow engine.

The release claim is bounded by the tests, conformance vectors, artifact checks,
and real-LLM scenarios recorded in `reports/`.
