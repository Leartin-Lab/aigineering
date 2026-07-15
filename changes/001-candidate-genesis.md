# Change 001: Candidate-native Genesis boundary

Status: Implementing
Target: 0.5.0 stable

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
- Complete: cryptography is a base dependency; the runtime does not silently
  substitute a non-authenticating deterministic seal.
- Pending: migrate remaining control-plane and worker submission effects.

## Required architecture tests

- Effective Genesis and Candidate payloads have deterministic IDs.
- Nested effect payloads are deeply immutable.
- Any domain, actor, key, content, or signature mismatch fails closed.
- Candidate receipt is distinct from effect acceptance.
- Runtime modules do not import a concrete Store adapter.
- Legacy files excluded from the wheel remain excluded until deleted.
- Direct fact-writing call sites monotonically decrease; no new exceptions.

## Deletion ledger

- `core/engine.py`, `core/startup_check.py`, `core/state_serializer.py`
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
