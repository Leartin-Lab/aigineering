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

1. A Contract is accepted through `RuntimeIngress.accept_contract`.
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

Assets and Contracts created by the CLI or control plane still enter through
trusted `RuntimeIngress` methods. They are not yet represented as signed typed
Candidates. Deterministic Asset seals provide replay integrity, not actor
authentication. These are known transition boundaries, not properties hidden
by the design documentation.

The transition API now also implements GenesisManifest, actor keys,
CandidateProposal, typed CandidateEffect values, canonical wire serialization,
and signature verification. `CandidateCommitter` supports one complete
`contract.declare` slice: it verifies the actor, applies the shared Contract
admission policy, and transactionally records receipt, acceptance or rejection,
audit evidence, and the Contract. `aig domain init` creates a local Ed25519 root
identity with a mode-0600 private key. `aig contract add` and `aig asset add`
publish only through signed `contract.declare` and `asset.propose` Candidates.
Asset commitment runs the same FactReducer consequences as compatibility
ingress, including activation and terminal records. Other control-plane
commands have not yet migrated.

`aig task create` is an aliasing user surface over the same
`contract.declare` publication path. CLI identity assembly is centralized in
one local Candidate publisher; the three commands do not each own signature or
Genesis-selection logic.

`aig behavior add` also publishes an ordinary `asset.propose` effect. Behavior
is therefore prompt/disclosure metadata on an Asset, not a separate commitment
primitive. Contract, Task, Asset, and Behavior commands share effect builders
and local identity selection.

The optional HTTP API accepts full signed CandidateProposal bodies at
`POST /candidates`, `POST /contracts`, and `POST /assets`. Resource endpoints
validate the effect type before commitment. Unsigned legacy request bodies fail
schema validation and cannot mutate the Store. Slice and replacement-claim HTTP
operations remain compatibility surfaces pending additional effect types.

`contract.publish` does not authorize an actor to populate
`minting_authority`. A declaration containing protected minting authority also
requires `contract.publish.protected`; payload fields cannot self-grant that
capability.

The commitment coordinator authenticates, dispatches, records decisions, and
commits atomically; it does not parse individual effect payloads. Built-in
effect projectors and Contract admission policy are separate pure modules. An
architecture gate keeps effect names and payload semantics out of the
coordinator and caps it below 300 source lines.

Candidate correctness does not depend on a process-local idempotency lock.
Candidate and derived consequence records have deterministic identities;
SQLite uniqueness and transactions arbitrate concurrent replicas. The
coordinator performs no pre-commit RuntimeRecord scan. FactReducer trace timing
is record metadata, not part of the derived semantic payload.

Candidate commitment uses the Store's existing atomic ingress transaction. A
process crash after physical Asset insertion but before Trace/RuntimeRecord
insertion rolls back the entire Candidate; restart observes neither a partial
fact nor a false receipt/terminal record.

A domain may persist exactly one `domain.genesis` RuntimeRecord. Initialization
is idempotent, replacement fails closed, SQLite enforces uniqueness, and a
CandidateCommitter can reconstruct the trust root from the Store instead of
receiving ambient process configuration. Genesis bootstrap is the sole direct
append exception in the candidate-native transition.

`cryptography` is a required runtime dependency because actor authentication is
a base security property. Deterministic content seals remain available for
integrity compatibility but are explicitly rejected as Candidate identity.

## Scheduling and reconstruction

Eligibility is derived from facts: input availability, activation expression,
terminal records, current claim, routing compatibility, and allowance. “Waiting”
is therefore a query result, not a durable Contract state. A restarted or backup
runtime can rebuild its projections from the shared Store and continue claims.

The shipped package excludes the legacy in-process Engine, startup checker, and
state serializer. Their source remains temporarily for migration tests. The
supported operational surface is the Store/RuntimeIngress/claim/submission path.

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
