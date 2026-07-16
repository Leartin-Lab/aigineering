# Aigineering 0.5 Design

This document describes the behavior implemented by the public repository.
Target designs belong in `changes/` until their code, tests, and migration are
complete. Architecture decisions explain why a durable choice exists; they do
not replace this description of the running system.

## Product boundary

Aigineering is a zero-trust runtime for turning untrusted worker output into
auditable facts. The runtime is asset-driven and append-oriented. A worker does
not mutate shared state: it claims a Contract, receives a disclosure-bounded
WorkerPackage, and submits a signed CandidateProposal containing one claim-bound
`worker.output` or `task.delegate` envelope. Projection and authority checks
decide which declared outputs may become Assets; delegation can only publish a
contained follow-up task while the source claim is valid.

The 0.5 reference implementation is single-domain and SQLite-first. SQLite WAL,
transactions, leases, and unique constraints provide the concurrency control;
the protocol does not rely on a process-local task lock.

## Implemented runtime path

1. An external root Contract is published as a signed `contract.declare`
   Candidate; legacy Method children remain a documented transition path.
2. An eligible worker atomically claims it and receives a WorkerPackage.
3. The worker signs raw output and claim metadata as one `worker.output` effect,
   or signs an explicit method action as `task.delegate`.
4. `submit_worker_candidate` authenticates the actor, capability, routing-key,
   package, worker, claim, lease, epoch, and idempotency bindings.
5. Pure projection parses the candidate and applies declared-output and
   reserved-namespace authority rules.
6. SQLite atomically commits accepted Assets, rejection/acceptance TraceEntry
   records, idempotency state, runtime records, and the claim transition.
7. RuntimeRecord replay reconstructs lifecycle projections. Trace is audit
   evidence; it is not a second mutable task state machine.

Replay is fail-loud on broken causal chains. A rejected projection without raw
Candidate evidence, or an expiration/provider-failure fact whose Contract is
missing, is a consistency error rather than skippable work. This prevents a
backup runtime from repeatedly observing the same unprocessed asset and ending
without either progress or a visible failure.

## Current data model

- Asset: immutable content with provenance metadata. Assets are runtime facts.
- Contract: immutable declaration of inputs, outputs, activation, allowance,
  routing requirements, and authority.
- CandidateEnvelope: immutable claim/package and raw-output payload nested
  inside a worker Candidate; the internal WorkerHost compatibility path still
  constructs it before signing is moved into the host.
- CandidateProposal: actor-authenticated typed effects used by external
  publishers and the external worker-submit protocol.
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

`POST /worker/submissions` likewise accepts only one signed claim-bound
`worker.output` or `task.delegate` CandidateProposal. Server claims require an
enabled actor/key-bound worker, so
an anonymous claimant cannot lock work it is unable to submit. The former
server-side mock `/contracts/{id}/run` mutation endpoint returns 410 and directs
clients to the claim/submission protocol; the server never impersonates a
worker actor.
`aig worker submit` uses the same authenticated, method-aware submission
service and default registry as HTTP, so a signed `/plan` output has identical
semantics at both ingress surfaces.

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

New Candidate-native registrations bind `worker_id` to the same `actor_id` and
an authorized, non-revoked `key_id`. `aig worker register` requires the public
key and commits actor authorization plus routing registration in one Candidate
batch; repeat versions may reuse only the exact existing key. SQLite schema v12
retains this binding in its disposable routing projection. Blank bindings are
legacy migration data, not valid new Candidate registrations.

Engine-as-Worker bootstraps each isolated invocation domain with an ephemeral
Ed25519 Genesis actor. Outer disclosure Assets and the inner root Contract are
published through the same identity-neutral Candidate publisher used by local
CLI composition; the adapter has no direct RuntimeIngress write path.

The commitment coordinator authenticates, dispatches, records decisions, and
commits atomically; it does not parse individual effect payloads. Built-in
effect projectors and Contract admission policy are separate pure modules. An
architecture gate keeps effect names and payload semantics out of the
coordinator and caps it below 300 source lines.

One Candidate may carry multiple effects as one atomic group. The pure
effect-batch projector checks every capability before commitment and rejects
the whole Candidate if any effect is invalid. A batch may publish multiple
Contracts and Assets. Fact reduction receives pending Contracts explicitly, so
activation and completion consequences are computed against the atomic
transaction view rather than effect insertion order. This is the ordinary
fan-out primitive used by task-producing plugins.

Candidate correctness does not depend on a process-local idempotency lock.
Candidate and derived consequence records have deterministic identities;
SQLite uniqueness and transactions arbitrate concurrent replicas. The
coordinator performs no pre-commit RuntimeRecord scan. FactReducer trace timing
is record metadata, not part of the derived semantic payload.

Candidate commitment uses the Store's existing atomic ingress transaction. A
process crash after physical Asset insertion but before Trace/RuntimeRecord
insertion rolls back the entire Candidate; restart observes neither a partial
fact nor a false receipt/terminal record.

Claim-bound external worker submission does not depend on RuntimeIngress. The
dedicated worker interpreter verifies the Candidate signature,
`worker.submit` capability, worker-to-key registration, and signed idempotency
binding before calling the same pure projection and Asset-fact reducer. SQLite
rechecks routing-key and claim predicates while atomically committing receipt,
output evidence, projection, lifecycle consequences, trace, idempotency, and
claim transition. `worker.output` and `task.delegate` are deliberately
unsupported by the generic effect committer, so `/candidates` cannot bypass
claim/package fencing. A WorkerHost uses the TaskDelegationPlugin to select the
delegation effect; signed method submissions cannot be reinterpreted from an
ordinary output effect. The same Store-free plugin projects every supported
delegation action (`plan`, `replan`, `tool`, `fail`, and `retry`) into its
contained child Contract and optional activation-context Asset; the runtime
transaction only commits that projection with the source claim transition.
The submission path no longer queries MethodRegistry to authorize delegation;
the plugin owns the closed supported-action set and rejects unknown actions.
MethodRegistry remains only in the completion compatibility layer while those
handlers move behind completion plugins. Worker execution, CLI submission, and
HTTP submission no longer accept or construct a method registry; task
publication is protocol behavior, not application handler configuration.
Application composition now names this residual surface
`default_completion_registry` and excludes retry: retry delegation creates its
ordinary replacement task immediately and has no system-task completion phase.
The application uses the minimal public `CompletionPlugin`/
`CompletionRegistry` protocol, which exposes only `handle_completion`; the old
MethodRegistry is confined to the source-only legacy Engine compatibility path.
Plan and replan completion now live in `plugins/planning_completion.py` and are
registered directly by application composition. Their old core handlers are
thin Engine scheduling adapters; the former replan copy remains only a
parameterized compatibility subclass.
Worker-produced tool observations use the small ToolCompletionPlugin;
application composition no longer imports the legacy ToolMethodHandler or its
in-process MCP/tool execution machinery. A multistep runtime test proves that
observation completion publishes its continuation as a separate signed
Candidate.
Explicit failure completion now lives in FailCompletionPlugin. It publishes the
protected failure report through its own Candidate actor and records the parent
as `failed` even if report publication is rejected. The application no longer
imports FailMethodHandler, and `/fail` can no longer leave a processed child
with a non-terminal, unsatisfied parent.
Production completion projection no longer constructs RuntimeIngress or a
FactReducer. MethodRuntime receives direct ingress only from the excluded legacy
Engine or explicit compatibility tests; without one, direct Contract/Asset
mutation fails and completion plugins must use their registered Candidate
publisher.
Local projection-, expiration-, provider-, and malformed-plan recovery now
receives the same plugin publisher registry. Recovery Contract plus protected
failure-context Asset commit as one signed `recovery.publish.v1` Candidate;
publication rejection remains visible and the failed source is still terminal.
Projection rejection records that failed terminal atomically with Candidate
commit. A separate projection-recovery progress fact tracks whether replacement
work was published, so terminality cannot be mistaken for completed recovery.
The runtime replay service cannot manufacture a trusted ingress when that
publisher is absent: explicit replay fails as a configuration error, while
external HTTP submission durably records the rejection for an independently
configured recovery runtime.
Durable local key provisioning now lives in application-level
`local_identity.py`, not CLI semantics; CLI commands share one fixed runtime
publisher registry. The HTTP submission endpoint never reads local private keys
or performs direct recovery. A rejected HTTP Candidate remains a replayable
fact for an independently configured recovery worker/runtime.
EngineWorker constructs the same publisher registry from its isolated inner
domain actor (without filesystem keys) and passes it through recovery,
completion, claim, and execution. Nested execution therefore does not regain a
direct recovery write path. Each selected inner delegate is authorized and
registered in that invocation domain, then submits through WorkerHost as a
signed Candidate; EngineWorker no longer imports the legacy MethodRegistry or
handler stack. Durable local and ephemeral nested composition share the same
identity-neutral WorkerHost authorization primitive rather than duplicating
registration policy.
Authentication, claim, policy, and binding failures append Candidate rejection
records and Trace evidence before returning an error; an invalid worker result
cannot disappear as an API-only failure.

Local CLI execution now uses `WorkerHost`. Each concrete worker adapter has a
durable delegated Ed25519 key and Candidate registration; the local root key
only authorizes that actor. Claims use the worker actor ID, ordinary output and
the transitional Method path both retain authenticated Candidate receipt and
typed output/delegation evidence, and SQLite applies the same key/claim fencing
to both. The raw
`execute_claimed_package(worker, ...)` form remains only as an internal test and
Method migration compatibility surface.

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

The public `TaskPlugin` protocol is now Store-free: a plugin receives a frozen,
disclosure-bounded `PluginRequest` (including an optional causal source task)
and returns a `PluginProposal` containing
ordinary Candidate effects plus visible containment notes. The first planning
expansion plugin converts a structured plan Asset into one atomic fan-out of
`contract.declare` effects, independently unit-testable before publication.
The continuation plugin converts a completed method task into one ordinary
follow-up `contract.declare` effect by the same rule.
Publication still uses the identity-neutral Candidate publisher and a plugin
actor key; plugins receive no trusted Store mutation handle.

The current source tree still contains plan, replan, retry, recovery, fail, and
tool Method handlers. They create explicit child Contracts rather than hidden
agent state, but they remain feature-specific runtime code and are part of the
0.5 refactor debt. The new planning plugin temporarily reuses the tested legacy
containment compiler until that code is physically moved out of `core.methods`.
Recovery task projection has moved to `plugins/recovery.py`; the old recovery
handler module is now a thin source-compatibility adapter, and shipped runtime
code no longer imports the handler directory.
The remaining compatibility handlers are still source-visible, but the local
LLM path and engine-backed inner execution now share the authenticated
WorkerHost protocol.

The local production task loop injects an immutable, plugin-id keyed registry of
actor-bound `CandidatePublisher` values into completion projection. This keeps
planning and continuation identities and capabilities distinct. Plan and replan results then
use the same PlanningExpansionPlugin and publish their entire child-task fan-out
as one signed Candidate; successful tool completion publishes its continuation
task through the continuation plugin as another signed Candidate. Publication
rejection is traced and closes the parent instead of leaving it suspended. The
two former near-copy handlers now share one
implementation; direct `runtime.add_contract` remains only when compatibility
tests deliberately omit a publisher.

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
