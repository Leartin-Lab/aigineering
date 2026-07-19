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
effect batch. `/exec` becomes ordinary `asset.propose` effects; `/plan` and
`/replan` become contained `contract.declare` effects. Projection and authority
checks decide which declared outputs or child obligations may become facts.

The 0.5 reference implementation is single-domain and SQLite-first. SQLite WAL,
transactions, leases, and unique constraints provide the concurrency control;
the protocol does not rely on a process-local task lock.

## Implemented runtime path

1. An external root Contract is published as a signed `contract.declare`
   Candidate; legacy Method children remain a documented transition path.
2. Across HTTP, an eligible Worker signs a single-use `worker.claim` Candidate;
   SQLite atomically records its authenticated request and claim before returning
   a WorkerPackage. Local WorkerHost coordination uses the same fenced Store
   operation without crossing a transport boundary.
3. WorkerHost translates `/exec` into `asset.propose`; `/plan` or `/replan`
   invokes the Store-free staged plugin and signs three `contract.declare`
   effects. Hosted `/tool`, `/fail`, and `/retry` likewise use Store-free local
   plugins and sign one ordinary contained `contract.declare`. CLI and HTTP
   submission accept the same claim-bound ordinary effect protocol.
4. Candidate identity binds the Contract, package, claim, epoch and effects.
   SQLite rechecks the registered actor key and live claim in the commit
   transaction.
5. Pure projection applies exact-output, protected-namespace and child
   containment rules before any fact exists.
6. SQLite atomically commits accepted Assets, rejection/acceptance TraceEntry
   records, idempotency state, runtime records, and the claim transition.
7. RuntimeRecord replay reconstructs lifecycle projections. Trace is audit
   evidence; it is not a second mutable task state machine.

Claim delegation cannot be amplified by child payload fields. A child may carry
protected outputs or minting templates only when they are inherited from the
claimed parent's existing `minting_authority`; declaring its own authority does
not grant `contract.publish.protected`. New planning intermediates and hosted
tool/failure results use ordinary content-isolated names, so they need no
reserved-namespace exception.

Replay is fail-loud on broken causal chains. A rejected projection without raw
Candidate evidence, or an expiration/provider-failure fact whose Contract is
missing, is a consistency error rather than skippable work. This prevents a
backup runtime from repeatedly observing the same unprocessed asset and ending
without either progress or a visible failure.

## Current data model

- Asset: immutable content with provenance metadata. Assets are runtime facts.
- Contract: immutable declaration of inputs, outputs, activation, allowance,
  routing requirements, and authority.
- CandidateEnvelope: Worker-adapter value used to normalize one raw action.
  WorkerHost converts it to ordinary typed effects before signing.
- CandidateProposal: actor-authenticated typed effects used by external
  publishers and Workers; an optional `CandidateClaimBinding` canonically binds
  pull execution to Contract/package/claim/epoch. Non-empty execution metadata
  is part of the canonical signed payload and receipt evidence.
- RuntimeRecord: versioned, content-addressed append-only event used for
  reconstruction.
- TraceEntry: human- and machine-readable audit evidence for decisions.
- Claim and WorkerPackage: ephemeral coordination records whose transitions are
  transactionally recorded and reconstructable.

### Causal allowance

`Contract.budget` is the migration input to an immutable root
`allowance.granted` fact. Publishing a child Contract atomically records an
`allowance.reserved` fact against its parent and a grant for the child. Planning,
execution, and verification reservations carry distinct purpose evidence. A
terminal Contract extinguishes its unreserved remainder; no remainder becomes a
Worker balance.

Runtime views derive available allowance as grants minus reservations and
extinguishments. Pure projection rejects an oversized batch, and SQLite repeats
the check after acquiring the commit transaction so concurrent child publication
and termination cannot overspend or over-extinguish the same lineage. Exact
Candidate replay cannot reserve twice. Legacy Contracts without grant facts
retain their declared budget only as a migration fallback.

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

`POST /worker/claims` and its renewal endpoint accept signed operational
Candidates whose actor/key matches the enabled Worker registration. Claim and
renew authentication records commit atomically with the lease transition, and
an accepted command Candidate cannot be replayed. `POST /worker/submissions`
accepts a signed claim-bound CandidateProposal whose batch contains only
`asset.propose` outputs or `contract.declare` expansion. Legacy `worker.output`
and `task.delegate` wrappers are rejected even with a valid claim binding.
Server claims require proof of the enabled actor/key-bound
Worker, so an anonymous or self-reported claimant cannot lock work it is unable
to submit. The former
server-side mock `/contracts/{id}/run` mutation endpoint returns 410 and directs
clients to the claim/submission protocol; the server never impersonates a
worker actor.
`aig worker submit` uses the same generic Candidate commitment service as HTTP.
The public effect-builder surface exposes only ordinary protocol effects; the
former `worker_output_effect` and `task_delegation_effect` wrapper constructors
have been removed, so new clients cannot accidentally generate the rejected
compatibility protocol.

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

Claim-bound Worker submission now uses the generic commitment reducer for
ordinary output and staged plan/replan effects. The signed Candidate carries an
exact `CandidateClaimBinding`; SQLite rechecks routing-key, Contract, package,
lease and epoch while atomically committing receipt, projected Facts, Trace,
`attempt.closed`, terminal consequences and claim transition. A successful
output attempt is `output_asserted`; planning is `expanded` and does not satisfy
the root; an invalid contained expansion is `failed`. Exact replay after claim
closure returns the same decision without duplicate facts. A different
Candidate against the closed claim fails the transaction fence and records a
visible rejection.
The plugin owns the closed supported-action set and rejects unknown actions.
The former MethodRegistry and feature-specific handlers have been deleted.
Worker execution, CLI submission, and HTTP submission do not construct a
method registry; task
publication is protocol behavior, not application handler configuration.
Application composition now names this residual surface
`default_completion_registry` and excludes retry: retry delegation creates its
ordinary replacement task immediately and has no system-task completion phase.
The raw-envelope `submit_candidate_envelope` and `_submit_claimed_method`
committers have been deleted. A successful bare Worker invocation is closed as
an unsigned-adapter failure; only an authenticated WorkerHost may submit work.
The former `core/submit.py` envelope committer has also been deleted; all
supported WorkerHost, CLI, and HTTP submissions now have one commitment owner,
`CandidateCommitter`.
SQLite likewise exposes one generic `commit_ingress_batch` Candidate transaction;
the former candidate-envelope and method-specialized Store commit operations are
not part of the runtime protocol.
The adapter remains one StorePort facade. Declarative DDL and historical v1-v13
migrations live in separate adapter-internal modules, so operational claim and
commit code cannot silently become a second schema owner. Migration backfills
still run in the existing schema transaction and reuse the facade's canonical
row materializers.
Candidate effect projection is similarly split by responsibility: individual
typed-effect projectors produce immutable fact proposals, while one batch
projector alone enforces atomic-group, capability, claim-containment and causal
allowance rules. `CandidateCommitter` calls only that batch entry point.
The application uses the minimal public `CompletionPlugin`/
`CompletionRegistry` protocol, which exposes only `handle_completion`.
Plan and replan completion now live in `plugins/planning_completion.py` and are
registered directly by application composition.
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
Production completion projection constructs neither RuntimeIngress nor a
FactReducer. The former MethodRuntime has been deleted; completion plugins use
their registered Candidate publisher.

Planning fan-out is atomic at the semantic boundary. A child or scaffold
rejection suppresses the entire proposed fan-out and creates explicit recovery
work; accepted siblings from the same invalid plan are not committed. Method
result Assets retain the Contract that declared them as `created_by`, and the
FactReducer ignores protected method-result names whose provenance does not
match a declaring Contract.
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

## Output acceptance

Contract identity optionally binds an `acceptance_policy`. The default or
`mechanical` mode preserves declared-output projection: an authorized assertion
can satisfy the slot. In `independent` mode, output presence alone never
completes the Contract. A distinct actor publishes `asset.attest` for one exact
Contract/output/Asset tuple and must hold both `asset.attest` and every
Contract-required verifier capability. The target must have been produced for
that Contract through its claim-bound Worker submission.

An accepted verdict records `asset.attested` and `output.qualified`; a rejected
verdict remains evidence without qualification. Qualification and the terminal
consequence commit atomically, bind the exact Asset ID, reconstruct after
restart, and converge under concurrent independent attestations. The 0.5
reference policy deliberately supports one independent acceptance, not quorum,
reputation, payment, or verifier-market semantics. `aig verify attest` uses a
separate locally authorized verifier actor rather than the producing/root
publication identity.

The legacy in-process Engine, state serializer, Method runtime/registry/handlers,
ContinuationManager, and context-overflow controller have been deleted. The superseded startup
checker has been deleted: expired claims and recovery are derived directly from
lease/runtime facts, without a second process-lifecycle trace state machine.
The supported operational surface is the Store/claim/submission path.

## Methods and workers

The public `TaskPlugin` protocol is now Store-free: a plugin receives a frozen,
disclosure-bounded `PluginRequest` (including an optional causal source task and
immutable invocation parameters)
and returns a `PluginProposal` containing
ordinary Candidate effects plus visible containment notes.
Invocation parameters participate in staged task identity and task description,
so distinct plan/replan requests cannot collapse to the same Candidate.
`StagedPlanningPlugin` and `StagedReplanningPlugin` publish draft,
dependency-analysis and compile as three ordinary Contracts in one atomic
Candidate group. Each stage has a distinct label, prompt schema, exact protected
intermediate output and independent test oracle. Draft output activates
dependency analysis; both intermediate facts activate compile. Planning reserves
one allowance unit for each intermediate stage and transfers the remaining
lineage allowance to compile. The compile Worker uses its Store-free local
planning plugin to turn one temporary `planning_blueprint` response directly
into claim-bound `contract.declare` effects. No `_plan_result_` Asset or
completion callback exists on the hosted staged path.
The continuation plugin converts a completed method task into one ordinary
follow-up `contract.declare` effect by the same rule.
Compile publication uses the claimed Worker key and limited claim delegation;
standalone continuation/recovery plugins use identity-neutral Candidate
publishers and explicit plugin actor keys. Plugins receive no trusted Store
mutation handle.

The tested containment compiler is physically owned by
`plugins/task_semantics.py` along with delegation and continuation projection.
`core.methods` is a thin
source-compatibility export, not a production semantics owner.
Recovery task projection lives in `plugins/recovery.py`. The 0.5.0 wheel and
sdist no longer contain a `RuntimeIngress`: setup, CLI, server, worker and
recovery publication all use signed Candidates and the shared commitment
reducer. Every module present in the wheel is importable without a legacy
mutation adapter.
The local LLM path and engine-backed inner execution share the authenticated
WorkerHost protocol.

Production completion projection is stateless. `TaskCompletionProjector`
reconstructs each pass from Contracts, Assets, terminal facts and the durable
`task_completion.projected` marker; it owns no suspended, resumed, or
method-scheduled process sets. Completion plugins receive a narrow
Candidate-publishing context, and a missing publisher fails closed instead of
restoring a direct mutation ingress.

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
Production loops invoke the neutral `process_task_completions` entrypoint. The
old Method-named function is only a source compatibility alias; it no longer
defines the runtime composition surface.
The completion consequence marker and its audit trace commit as one Store
batch. Terminal consequence emission checks both immutable terminal facts and
historical terminal traces, and commits a new terminal fact and trace together;
a later satisfied child cannot change a failed parent into complete.
Plan/replan expansion commits `attempt.closed(outcome=expanded)` and an
`expanded` audit event, not waiting or method-delegation state. Runtime
projections and SQLite still read historical Method-named records for database
compatibility while the remaining action adapters are removed.
There is no persisted waiting/task-state row. `RuntimeProjection` derives one
enabled boolean from terminal facts, output/input/activation satisfaction,
budget, attempt/delegation facts, and the claim lease. A staged root projects as
`expanded` until descendant facts satisfy it; historical delegation tasks may
still project as `blocked_delegation` during migration.
One scheduler pass derives all Contract views from one immutable runtime-record
snapshot; this changes no facts or state semantics and avoids reconstructing the
same SQLite log once per Contract.
Once a terminal fact exists, the blocker projection contains only that terminal
explanation; historical claims or delegation facts do not appear as current
work blockers.

## Active change

`changes/001-candidate-genesis.md` migrates the runtime toward signed typed
Candidates, a Genesis trust root, one commitment reducer, and plugin-produced
ordinary tasks. Until that change closes, this file remains authoritative about
what is actually supported.

`changes/002-post-review-boundary-hardening.md` records the 0.5 review closure
for authenticated Worker coordination, atomic completion projection, terminal
single assignment, and atomic planning recovery.

ADR-011 records the stable Candidate-native and plugin direction. It does not
make unfinished migration items part of current runtime truth.

## Verification evidence

Architecture constraints live in `tests/architecture/`. Release-grade evidence
is retained in `reports/`; ordinary test output and exploratory notes are not.
