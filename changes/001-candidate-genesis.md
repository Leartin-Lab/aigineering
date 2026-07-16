# Change 001: Candidate-native Genesis boundary

Status: Implementing
Target: 0.5.0 stable
Public decision: `docs/adr/ADR-011-candidate-native-plugin-runtime.md`

## Problem

The commitment boundary is strong for LLM worker submissions, but creation of
Contracts, control Assets, method subtasks, and worker registrations still uses
several trusted Python entry points. CandidateEnvelope is also a raw-output,
claim-specific type rather than a common authenticated proposal protocol.
Consequently “all changes are Candidates” is not yet true, actor authority is
partly process identity, and feature-specific Method code inflates the kernel.

## Resulting design

- GenesisManifest identifies a domain, its root actor keys, and its initial
  policy hash.
- CandidateProposal is immutable, content-addressed, actor-signed, causal, and
  contains one or more typed CandidateEffects.
- Signature verification only admits a Candidate as `candidate.received`; it
  does not make any proposed effect a fact.
- One commitment reducer validates policy, authority, references, allowance,
  and atomic groups, then emits accepted/rejected append-only records.
- Contracts, Assets, claims, worker registrations, policy changes, and plugin
  task publication become typed effects on that same path.
- Plan/replan/recovery/tool behavior becomes plugins that publish ordinary
  Contracts. A worker may plan a planning task recursively because the kernel
  does not assign special lifecycle semantics to “method” names.

## Compatibility sequence

1. Add canonical Genesis and signed Candidate protocol values plus verification.
2. Add the commitment reducer beside the existing submit path and prove
   equivalent declared-output behavior.
3. Migrate root Contract publication, then worker submission, then control
   Assets and registration.
4. Move method behavior behind plugins and ordinary Candidate effects.
5. Delete direct RuntimeIngress fact creation and legacy Engine/Method source.
6. Make reconstruction, active-active claiming, rejection visibility, and build
   artifact inspection release gates.

No compatibility adapter may silently turn a rejected new-protocol Candidate
into a legacy direct write.

## First vertical slice

The first slice introduced by this change is deliberately non-committing:

1. construct a GenesisManifest;
2. sign a typed CandidateProposal;
3. verify domain, identity, key, content ID, and signature;
4. produce a `candidate.received` RuntimeRecord.

It proves the authentication boundary without prematurely adding a second
commit path. Projection and persistence integration follows only after reducer
tests exist.

Implementation progress:

- Complete: canonical Genesis, signed Candidate, wire round trip, fail-closed
  authentication, and non-committing receipt record.
- Complete: pure and store-independent `contract.declare` reducer with atomic
  Memory/SQLite commitment, idempotent replay, and visible rejection records.
- Complete: legacy Contract ingress delegates to the same admission policy, so
  the compatibility path no longer owns a divergent copy of those rules.
- Complete: Genesis is an immutable, reconstructable Store record; SQLite
  schema v8 enforces one Genesis per domain Store and the committer loads it
  without process-local trust state.
- Complete: `aig domain init` persists an Ed25519 root identity and mode-0600
  private key; `aig contract add` uses the signed Candidate path and refuses to
  run without initialization or with an unauthorized local key.
- Complete: `asset.propose` uses the same reducer, transaction, rejection, and
  FactReducer materialization path; `aig asset add` no longer uses direct
  RuntimeIngress acceptance.
- Complete: shared FactReducer materialization was extracted from
  RuntimeIngress, and same-Store trace writes no longer create duplicate
  `trace.recorded` records.
- Complete: `aig task create` shares `contract.declare`; local CLI actor/key
  selection and rejection handling are centralized rather than copied across
  Contract, Task, and Asset commands.
- Complete: Candidate-level idempotency preserves full Trace payloads needed
  for byte-equivalent projection reconstruction instead of discarding recording
  metadata to force hash equality.
- Complete: effect parsing/projection and Contract admission are separated from
  the commitment coordinator. The coordinator fell from 410 to 265 lines, and
  an architecture test prevents effect-specific branches or renewed growth
  beyond 300 lines.
- Complete: `aig behavior add` reuses `asset.propose`; shared CLI effect
  builders remove duplicated wire-payload assembly from all four migrated
  publication commands.
- Complete: two independent SQLite connections can commit the same Asset
  Candidate concurrently while producing one Asset, one Candidate receipt/head,
  and one terminal consequence. Deterministic reducer consequences make the
  database transaction the correctness boundary; the former linear preflight
  RuntimeRecord scan has been deleted and is prohibited by architecture test.
- Complete: process-level crash injection at
  `after_asset_before_trace` proves Candidate Asset, receipt, trace, and terminal
  consequences roll back as one SQLite transaction.
- Complete: HTTP Contract/Asset creation and the generic `/candidates` endpoint
  require full actor-signed CandidateProposal bodies. Resource/effect mismatch
  and unsigned legacy bodies are rejected before Store mutation.
- Complete: Contract projection derives an additional
  `contract.publish.protected` capability requirement from effective
  `minting_authority`, closing payload-based protected-namespace escalation.
- Complete: recovery recreation no longer writes Contracts through
  RuntimeIngress; it uses the local signed Candidate publisher. Cancellation is
  intentionally still pending a typed lifecycle effect.
- Complete: capability and MCP descriptor commands publish protected
  `asset.propose` Candidates. Protected Asset names derive the independent
  `asset.publish.protected` capability and feature-specific direct Trace writes
  were removed.
- Complete: the quick demo's provider configuration, input Assets, and root
  Contract publish as signed Candidates. First-run local Genesis/key creation
  remains the single explicit bootstrap exception, and audit export no longer
  duplicates traces already written by commitment.
- Complete: standard effect payload builders live in the protocol layer, so
  runtime composition can publish Candidates without importing CLI semantics.
- Complete: SkillLoader is a pure filesystem-to-Asset builder. The skill CLI
  owns publication and commits both protected descriptor and content Assets as
  signed Candidates without feature-specific injection Trace records.
- Complete: worker routing metadata publishes through a capability-gated
  `worker.register` Candidate effect. Store commitment atomically appends its
  immutable fact and updates the rebuildable current routing projection; CLI
  registration no longer calls Store mutation directly.
- Complete: local and HTTP Asset slicing publish `asset.propose` Candidates
  with preserved lineage. The HTTP convenience route recomputes the expected
  slice and rejects unsigned or mismatched proposed payloads before mutation.
- Complete: replacement assertions publish through `asset.relate`; actor
  identity is derived from the verified Candidate, not request metadata, and
  Stores derive the claim index from immutable `replacement.claimed` records.
- Complete: Engine-as-Worker creates an invocation-scoped Genesis identity and
  publishes disclosed inputs and its inner root Contract as Candidates through
  an identity-neutral publisher shared with CLI composition.
- Complete: `control_plane` is now pure proposal construction. Unexported
  `inject_asset`/`inject_contract` compatibility functions and their duplicate
  direct-ingress test suite were deleted after production callers reached the
  Candidate boundary.
- Complete: the unshipped startup checker and its process-lifecycle Trace state
  machine were deleted. Supported recovery already derives orphan handling
  from claim leases and immutable runtime facts.
- Complete: recovery cancellation publishes a `contract.cancel` Candidate.
  SQLite schema v9 and MemoryStore enforce one terminal fact per Contract, and
  recovery discovery derives resolution from records/contracts rather than
  mutable Trace interpretation.
- Complete: claim-bound worker submission no longer constructs or accepts a
  RuntimeIngress dependency. CandidateProposal commitment, CandidateEnvelope
  submission, and compatibility ingress share one pure Asset-fact reduction
  function, removing a duplicate commitment coordinator from the supported
  worker path.
- Complete: the operator retry command publishes its deterministic retry
  Contract through `contract.declare`; the original attempt is bound as a
  Candidate causal parent. CLI retry no longer instantiates MethodRuntime or a
  feature-specific handler.
- Complete: a root actor with `actor.authorize` can publish an immutable
  `actor.authorized` key fact, after which that actor can authenticate its own
  Candidates. MemoryStore and SQLite schema v10 reject actor/key rebinding;
  deterministic content seals cannot be authorized as actor keys.
- Complete: `actor.revoke` is a capability-gated, single-assignment key fact.
  Candidate authentication projects revoked keys explicitly, and Store commit
  rechecks each authenticated receipt against revocation inside the transaction.
  A repeated active-active race proves a result either precedes revocation or
  is fenced; it cannot commit after revocation.
- Complete: a key with `actor.rotate` can atomically authorize a replacement
  key and revoke itself. Rotation is self-only, requires a new key ID, and the
  replacement capabilities must be a subset of the signing key's capabilities;
  a mid-commit failure rolls both facts back.
- Complete: Candidate commitment accepts a Candidate-wide atomic effect batch.
  Effect dispatch, capability checks, and batch composition stay outside the
  coordinator. Mixed atomic-group IDs fail closed; any invalid effect rejects
  the complete batch.
- Complete: multiple Contracts and their activating/result Assets may publish
  in the same Candidate. The pure FactReducer accepts pending Contracts as an
  explicit transaction view, preserving Memory/SQLite parity and providing the
  generic fan-out primitive for plugin-produced ordinary tasks.
- Complete: new `worker.register` Candidates require `worker_id == actor_id`
  and an authorized, non-revoked key binding. The CLI atomically authorizes a
  supplied public key and registers its routing profile; SQLite schema v12
  persists the binding in the rebuildable worker projection. Legacy blank
  bindings remain readable only for explicit migration compatibility.
- Complete: cryptography is a base dependency; the runtime does not silently
  substitute a non-authenticating deterministic seal.
- Complete: external claim-bound worker submission accepts only an
  actor-signed CandidateProposal containing one `worker.output` or
  `task.delegate` effect. The
  actor must have `worker.submit`, match the registered worker/key binding, and
  sign the same non-empty idempotency key as the embedded envelope. SQLite
  rechecks key binding with the claim predicate in the commitment transaction;
  authenticated receipt, output evidence, and projection form one causal chain.
  The generic Candidate committer intentionally rejects both claim-bound effect
  types so they cannot become a claim-bypass path. Authentication and post-authentication
  submission failures use the same durable Candidate rejection vocabulary and
  Trace evidence rather than ending as caller-only errors.
- Complete: a Store-free public TaskPlugin protocol separates pure proposal
  construction from actor-authenticated publication. The planning expansion
  plugin turns one disclosed plan Asset into an atomic fan-out of ordinary
  `contract.declare` effects; its worker behavior and Candidate integration are
  tested independently. It temporarily delegates to the existing containment
  compiler while that implementation is moved out of `core.methods`.
- Complete: WorkerHost binds a stateless execution adapter to one actor key and
  signs its claim-bound envelope. Local CLI workers persist delegated keys,
  register by Candidate, claim as that actor, and use the signed path for both
  ordinary output and transitional Method actions. SQLite key fencing is shared
  by ordinary and Method submission transactions.
- Complete: WorkerHost selects `task.delegate` through a pure adapter plugin for
  explicit method actions; the claim-bound runtime rejects attempts to
  reinterpret a signed ordinary `worker.output` as delegation. Delegation
  receipt and method scheduling are a typed causal chain.
- Complete: the TaskDelegationPlugin, rather than the runtime service, projects
  plan/replan/tool/fail/retry requests into a contained child task and optional
  context Asset. Each supported method type has the same plugin-level
  conformance test; SQLite still commits the projection and claim transition
  atomically.
- Complete: claim-bound task publication no longer depends on a registered
  MethodHandler. The delegation plugin owns the closed supported-action set;
  MethodRegistry is now completion-only transition debt rather than task
  publication authority.
- Complete: method-registry parameters were removed from worker execution and
  both submission ingress surfaces. The registry is now confined to legacy
  completion projection instead of being threaded through every worker call.
- Complete: application composition exposes a completion registry, not a
  method-publication registry. Retry was removed from it because retry is fully
  projected by TaskDelegationPlugin and has no completion behavior.
- Complete: application/runtime completion projection uses a minimal
  CompletionPlugin/CompletionRegistry protocol with no task-publication API.
  The old MethodRegistry is now referenced only by the source-only legacy
  Engine compatibility path.
- Complete: plan/replan completion projection moved out of core Method handlers
  into application-registered completion plugins. The legacy plan handler is a
  small source-only Engine scheduling adapter, and replan remains a parameter
  specialization rather than duplicated logic.
- Complete: supported runtime tool completion uses ToolCompletionPlugin to
  acknowledge a worker-produced declared observation and publish a signed
  continuation Candidate. Application composition no longer imports the legacy
  in-process ToolMethodHandler execution stack.
- Complete: FailCompletionPlugin publishes protected failure reports through a
  dedicated Candidate actor and explicitly fails the unfinished parent. This
  closes the prior silent state where the fail child was marked processed while
  the parent had neither outputs nor a terminal fact. Application composition
  no longer imports FailMethodHandler.
- Complete: production completion projection has no RuntimeIngress/FactReducer
  dependency. Direct compatibility mutation now requires explicit ingress;
  Candidate-native completion cannot silently manufacture one when a publisher
  is missing.
- Complete: local recovery replay publishes its recovery Contract and protected
  failure-context Asset atomically through a dedicated recovery plugin actor.
  Rejected projection, expired claim, provider failure, and malformed planning
  result paths all accept the same explicit publisher registry.
- Complete: local actor/plugin key persistence moved out of the CLI layer into
  application-level local identity composition. HTTP worker ingress neither
  provisions server-local private keys nor falls back to direct recovery; its
  rejected Candidate fact is available for an independently configured replay
  runtime.
- Complete: EngineWorker derives an in-memory publisher registry from its
  isolated domain actor and threads it through recovery, completion, claim, and
  execution. Nested runtimes no longer fall back to direct recovery mutation.
- Complete: EngineWorker authorizes and registers each selected delegate in its
  invocation domain, submits through WorkerHost, and composes the application
  CompletionPlugin registry. It no longer imports MethodRegistry or the legacy
  handler stack.
- Complete: durable local workers and invocation-scoped nested workers use one
  identity-neutral WorkerHost authorization/registration primitive; key
  persistence remains an outer application concern.
- Complete: recovery task projection moved from the legacy Method handler
  directory into the plugin layer. Runtime and planning completion no longer
  import handler implementations; the old module only preserves source
  compatibility.
- Complete: plan containment, delegation, continuation, retry, method-context,
  and system-asset construction moved together into plugin-owned pure task
  semantics. Production plugins and worker adapters no longer import
  `core.methods`; that module is a small compatibility export only.
- Complete: release artifacts exclude legacy Engine, RuntimeIngress,
  MethodRegistry/handlers, context-overflow controller, and state serializer.
  A built-wheel audit imports every shipped module after those exclusions.
- Complete: CLI and EngineWorker completion loops use the neutral
  `process_task_completions` API. The Method-named function remains only as a
  forwarding source compatibility alias.
- Complete: recovery replayers fail loudly when durable rejection, expiration,
  or provider-failure facts lack the Contract/raw Candidate evidence required
  to derive their consequence. These broken causal chains can no longer be
  silently skipped forever on every restart.
- Complete: recovery replay itself requires an authenticated
  `recovery.publish.v1` publisher. Production runtime code no longer imports
  RuntimeIngress or FactReducer as a fallback; external submission records a
  replayable rejection instead of manufacturing a local private identity.
- Complete: a fully rejected projection commits the source task's failed
  terminal fact atomically. Recovery progress has its own immutable marker, so
  absence of a recovery publisher is visible without falsely claiming that
  replacement work was scheduled.
- Complete: the HTTP worker submission endpoint accepts signed Candidates only;
  server claims require an enabled actor-key binding. The server-side mock run
  endpoint no longer impersonates a worker or mutates runtime state.
- Complete: CLI and HTTP worker submission share one authenticated,
  method-aware service. Signed delegation output no longer changes behavior
  according to ingress surface.
- Complete: the production task loop injects a durable planning-plugin actor
  through CandidatePublisher. Plan and replan completion publish contained
  child fan-out as one signed Candidate; their duplicated handlers collapse to
  one parameterized implementation. Direct Contract insertion is now an
  explicit no-publisher compatibility branch rather than the production path.
- Complete: production completion projection uses an immutable plugin publisher
  registry rather than one ambient publisher. Planning and continuation have
  separate durable actor identities and capabilities. Successful tool completion
  proposes its ordinary continuation task through a Store-free plugin and signed
  Candidate; rejected publication is traced and terminates the parent instead of
  silently leaving suspended work.
- Pending: replace the remaining Method-named completion types with neutral
  task/plugin names, then delete the raw execution compatibility surface and
  source-only handlers.

## Required architecture tests

- Effective Genesis and Candidate payloads have deterministic IDs.
- Nested effect payloads are deeply immutable.
- Any domain, actor, key, content, or signature mismatch fails closed.
- Candidate receipt is distinct from effect acceptance.
- Runtime modules do not import a concrete Store adapter.
- Legacy files excluded from the wheel remain excluded until deleted.
- Direct fact-writing call sites monotonically decrease; no new exceptions.

## Deletion ledger

- `core/engine.py`, `core/state_serializer.py`
- feature-specific Method runtime/registry/handlers after plugin migration
- `RuntimeIngress.accept_asset` and `accept_contract` after Candidate adapters
  have no callers
- deterministic provenance seal as an authorization mechanism (it may remain a
  content checksum)
- duplicated protected-prefix and terminal-state logic

## Exit criteria

- Every external state change enters as an authenticated Candidate effect.
- Exactly one reducer owns Candidate-to-fact commitment.
- An arbitrary compatible Store can reconstruct all scheduling projections.
- Backup runtimes can safely share claims and load without process-local state.
- All rejection and terminal paths are visible and replayable.
- Unit, architecture, conformance, restart, active-active, live-worker, lint,
  format, build, and artifact tests pass.
- `DESIGN.md` is updated and this change is marked Complete.
