# Aigineering 0.5 Design

This document describes the behavior implemented by the public repository.
Target designs belong in `changes/` until their code, tests, and migration are
complete. Architecture decisions explain why a durable choice exists; they do
not replace this description of the running system.

## Product boundary

Aigineering is a zero-trust runtime for turning untrusted worker output into
auditable facts. The runtime is asset-driven and append-oriented. A worker does
not mutate shared state: it claims a Contract, receives a disclosure-bounded
WorkerPackage, and submits a CandidateEnvelope. Projection and authority checks
decide which declared outputs may become Assets.

The 0.5 reference implementation is single-domain and SQLite-first. SQLite WAL,
transactions, leases, and unique constraints provide the concurrency control;
the protocol does not rely on a process-local task lock.

## Implemented runtime path

1. An external root Contract is published as a signed `contract.declare`
   Candidate; legacy Method children remain a documented transition path.
2. An eligible worker atomically claims it and receives a WorkerPackage.
3. The worker returns raw output in a claim-bound CandidateEnvelope.
4. `submit_candidate` validates package, worker, claim, lease, epoch, and
   idempotency bindings.
5. Pure projection parses the candidate and applies declared-output and
   reserved-namespace authority rules.
6. SQLite atomically commits accepted Assets, rejection/acceptance TraceEntry
   records, idempotency state, runtime records, and the claim transition.
7. RuntimeRecord replay reconstructs lifecycle projections. Trace is audit
   evidence; it is not a second mutable task state machine.

## Current data model

- Asset: immutable content with provenance metadata. Assets are runtime facts.
- Contract: immutable declaration of inputs, outputs, activation, allowance,
  routing requirements, and authority.
- CandidateEnvelope: claim-bound untrusted worker response. In the current
  path its semantic body is raw model output, not typed effects.
- CandidateProposal: actor-authenticated typed effects used by external
  Contract and Asset publishers.
- RuntimeRecord: versioned, content-addressed append-only event used for
  reconstruction.
- TraceEntry: human- and machine-readable audit evidence for decisions.
- Claim and WorkerPackage: ephemeral coordination records whose transitions are
  transactionally recorded and reconstructable.

## Commitment boundary

The non-negotiable rules are specified in `docs/boundary-invariants.md` and
enforced by tests. In particular, worker output is never a fact; projection is
pure; undeclared and protected outputs are rejected visibly; and SQLite commits
the effects of a submission atomically.

Migrated CLI and HTTP publishers represent Contracts and Assets as signed typed
Candidates. Remaining slice, replacement-claim, Method-child, and raw worker
submission compatibility paths are known transition boundaries, not properties
hidden by the design documentation. Deterministic Asset seals provide replay
integrity, not actor authentication.

The control-plane module only builds immutable Asset and Contract proposals; it
has no Store, trace, or ingress dependency. The former `inject_asset` and
`inject_contract` compatibility API was removed after all production callers
migrated, eliminating a second trusted commitment surface.

The transition API now also implements GenesisManifest, actor keys,
CandidateProposal, typed CandidateEffect values, canonical wire serialization,
and signature verification. `CandidateCommitter` supports one complete
`contract.declare` slice: it verifies the actor, applies the shared Contract
admission policy, and transactionally records receipt, acceptance or rejection,
audit evidence, and the Contract. `aig domain init` creates a local Ed25519 root
identity with a mode-0600 private key. `aig contract add` and `aig asset add`
publish only through signed `contract.declare` and `asset.propose` Candidates.
Asset commitment runs the same FactReducer consequences as compatibility
ingress, including activation and terminal records.

`aig task create` is an aliasing user surface over the same
`contract.declare` publication path. CLI identity assembly is centralized in
one local Candidate publisher; the three commands do not each own signature or
Genesis-selection logic.

`aig behavior add` also publishes an ordinary `asset.propose` effect. Behavior
is therefore prompt/disclosure metadata on an Asset, not a separate commitment
primitive. Contract, Task, Asset, and Behavior commands share protocol-level
effect builders and local identity selection.

The quick demo performs Genesis/key creation only when the local domain is
absent. That one bootstrap is the explicit exception; provider configuration,
input Assets, and the demo Contract are then published through the same signed
Candidate commitment path. A separate audit export deduplicates durable Trace
entries by identity instead of becoming a second execution store.

Skill discovery is also separated from commitment: `SkillLoader` only parses
files and builds proposed Assets. The CLI publishes its protected descriptor
and prompt content through `asset.propose`, so filesystem parsing has no Store
or ingress dependency.

Asset slicing is a deterministic client-side transformation followed by an
ordinary signed `asset.propose`. The effect preserves lineage metadata. The
HTTP convenience endpoint accepts that complete signed proposal and verifies
its payload against the requested source/range before commitment; unsigned
slice requests cannot mutate the Store.

Replacement/equivalence assertions use the capability-gated `asset.relate`
effect. Commitment records the authenticated actor as claimant and the Store
derives its claim index from `replacement.claimed`; verification of referenced
content remains a separate operation, so actor authority cannot manufacture
equivalence. CLI and HTTP publishers validate currently referenced Assets and
never accept an ambient `signed_by` string.

The optional HTTP API accepts full signed CandidateProposal bodies at
`POST /candidates`, `POST /contracts`, and `POST /assets`. Resource endpoints
validate the effect type before commitment. Unsigned legacy request bodies fail
schema validation and cannot mutate the Store. Slice and replacement-claim HTTP
operations remain compatibility surfaces pending additional effect types.

`contract.publish` does not authorize an actor to populate
`minting_authority`. A declaration containing protected minting authority also
requires `contract.publish.protected`; payload fields cannot self-grant that
capability.

Administrative `aig recover --recreate` publishes its replacement Contract via
the same signed `contract.declare` path. `aig recover --cancel` publishes a
capability-gated `contract.cancel` effect; actor identity and reason are bound
to the Candidate. Recovery resolution is derived from terminal RuntimeRecords
or recovery child Contracts rather than Trace state.

Contract terminal facts are single-assignment. MemoryStore validates this
invariant before append, and SQLite schema v9 adds a unique expression index on
the terminal record's contract ID, so competing replicas cannot commit
different terminal outcomes.

Administrative `aig retry` is also ordinary Contract publication. It builds a
deterministic security-equivalent retry Contract, publishes `contract.declare`,
and binds the original attempt as a Candidate causal parent; it does not invoke
the legacy Method runtime.

Protected capability and MCP descriptor Assets reuse `asset.propose`. A
protected name derives an additional `asset.publish.protected` requirement;
ordinary asset publishers cannot acquire it from payload fields. The local
Genesis owner is explicitly granted this administrative capability.

Worker routing registration uses a dedicated `worker.register` effect and
capability. Commitment atomically appends the immutable registration fact and
the Store applies that RuntimeRecord to its rebuildable routing projection in
the same transaction; neither the generic commitment decision nor its batch
port contains worker-specific fields. The CLI no longer writes routing state
directly, and registration version is explicit at publication.

Engine-as-Worker bootstraps each isolated invocation domain with an ephemeral
Ed25519 Genesis actor. Outer disclosure Assets and the inner root Contract are
published through the same identity-neutral Candidate publisher used by local
CLI composition; the adapter has no direct RuntimeIngress write path.

The commitment coordinator authenticates, dispatches, records decisions, and
commits atomically; it does not parse individual effect payloads. Built-in
effect projectors and Contract admission policy are separate pure modules. An
architecture gate keeps effect names and payload semantics out of the
coordinator and caps it below 300 source lines.

One Candidate may now carry multiple effects as one atomic group. The pure
effect-batch projector checks every capability before commitment and rejects
the whole Candidate if any effect is invalid. The current vertical slice
supports at most one Contract and rejects Contract-plus-Asset batches because
newly declared Contract consequences do not yet share the Asset reducer's
transaction view; this limitation is explicit rather than order-dependent.

Candidate correctness does not depend on a process-local idempotency lock.
Candidate and derived consequence records have deterministic identities;
SQLite uniqueness and transactions arbitrate concurrent replicas. The
coordinator performs no pre-commit RuntimeRecord scan. FactReducer trace timing
is record metadata, not part of the derived semantic payload.

Candidate commitment uses the Store's existing atomic ingress transaction. A
process crash after physical Asset insertion but before Trace/RuntimeRecord
insertion rolls back the entire Candidate; restart observes neither a partial
fact nor a false receipt/terminal record.

Claim-bound worker submission does not depend on RuntimeIngress. It calls the
same pure Asset-fact reduction function used by typed Candidate commitment,
then commits projection, lifecycle consequences, trace evidence, idempotency,
and claim transition through the operational Store transaction.

A domain may persist exactly one `domain.genesis` RuntimeRecord. Initialization
is idempotent, replacement fails closed, SQLite enforces uniqueness, and a
CandidateCommitter can reconstruct the trust root from the Store instead of
receiving ambient process configuration. Genesis bootstrap is the sole direct
append exception in the candidate-native transition.

Genesis root actors may delegate Candidate capabilities with the
`actor.authorize` effect. Accepted public keys are immutable
`actor.authorized` facts; Candidate authentication derives its effective key
set from Genesis plus those facts. SQLite schema v10 and the in-memory adapter
forbid rebinding one actor/key identity to different key material.
`actor.revoke` adds a single-assignment revocation fact; SQLite schema v11 and
Store commit-time validation fence Candidates from revoked keys under
active-active races. `actor.rotate` atomically authorizes a replacement key and
revokes the signing key. It is self-only and cannot widen the actor's existing
capability set; transaction rollback prevents partial rotation.

`cryptography` is a required runtime dependency because actor authentication is
a base security property. Deterministic content seals remain available for
integrity compatibility but are explicitly rejected as Candidate identity.

## Scheduling and reconstruction

Eligibility is derived from facts: input availability, activation expression,
terminal records, current claim, routing compatibility, and allowance. “Waiting”
is therefore a query result, not a durable Contract state. A restarted or backup
runtime can rebuild its projections from the shared Store and continue claims.

The shipped package excludes the legacy in-process Engine and state serializer;
their source remains temporarily for migration tests. The superseded startup
checker has been deleted: expired claims and recovery are derived directly from
lease/runtime facts, without a second process-lifecycle trace state machine.
The supported operational surface is the Store/claim/submission path.

## Methods and workers

The current source tree still contains plan, replan, retry, recovery, fail, and
tool Method handlers. They create explicit child Contracts rather than hidden
agent state, but they remain feature-specific runtime code and are part of the
0.5 refactor debt. LLM, human, script, plugin, and engine-backed executors do not
yet share one authenticated actor protocol.

## Active change

`changes/001-candidate-genesis.md` migrates the runtime toward signed typed
Candidates, a Genesis trust root, one commitment reducer, and plugin-produced
ordinary tasks. Until that change closes, this file remains authoritative about
what is actually supported.

ADR-011 records the stable Candidate-native and plugin direction. It does not
make unfinished migration items part of current runtime truth.

## Verification evidence

Architecture constraints live in `tests/architecture/`. Release-grade evidence
is retained in `reports/`; ordinary test output and exploratory notes are not.
