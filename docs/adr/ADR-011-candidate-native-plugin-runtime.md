# ADR-011: Candidate-native plugin runtime

Status: Accepted; migration in progress
Date: 2026-07-15
Scope: v0.5 commitment boundary and v1 protocol direction
Related: ADR-005, ADR-006, ADR-007, ADR-008, ADR-009, ADR-010, ADR-013

## Context

The v0.5 boundary correctly treats LLM output as a Candidate, but trusted
control-plane calls, feature-specific Methods, and process identity still create
multiple classes of mutation. That split makes the kernel larger, makes human,
script, LLM, plugin, and nested-engine actors behave differently, and weakens
reconstruction across runtimes.

The protocol needs one answer to two questions:

1. Who proposed this change?
2. Which policy accepted it as a fact?

Prompt shape, model provider, UI, transport, and plugin implementation must not
change those answers.

## Decision

### One proposal protocol

Except for initial Genesis bootstrap, every external state change is an
immutable, content-addressed, actor-signed Candidate containing typed effects.
Signature verification admits a Candidate for evaluation; it never accepts an
effect by itself.

One commitment reducer applies capability, authority, reference, allowance,
atomic-group, and effect policy. It emits append-only accepted or rejected
records. There is no fallback from a rejected Candidate to a trusted direct
write.

### Genesis and actors

Each fact domain has one immutable Genesis manifest containing root public keys
and initial policy identity. Human users, scripts, LLM workers, plugins, and
engine-backed workers are actors distinguished by keys and capabilities, not by
privileged Python call paths. Deterministic content seals are checksums, not
actor authentication.

Workers remain stateless protocol participants. Identity and authority belong
to signed records; they are not an account balance or mutable worker session.

### Tasks and state

Task declarations are ordinary typed effects and durable records. Eligibility,
blocking, completion, claims, and allowance are projections of immutable facts.
“Waiting” is not a stored task state. Trace is audit evidence; RuntimeRecord is
the canonical replay envelope. Materialized tables and indexes may be deleted
and rebuilt.

Concurrency correctness belongs to the Store transaction and uniqueness/fencing
rules. No process-local Engine lock or preflight scan may be required for
Candidate idempotency. An Engine can therefore restart, run as a backup, share
load, or act as a Worker over the same protocol.

### Plugins instead of Methods

Plan, replan, recovery, verification, tool use, and domain extensions are
plugins that propose ordinary Candidates and task declarations. The kernel does
not assign lifecycle semantics to method names. Recursive planning is ordinary
task recursion.

Planning may use multiple explicit tasks—for example draft, dependency
analysis, and structured publication—so each stage has independently testable
inputs, outputs, authority, and acceptance evidence.

### Allowance and acceptance

Allowance limits how much computation a task may consume or delegate. It is not
a persistent worker wallet. A planner may restructure work within inherited
allowance but may not mint authority or budget.

ADR-013 specifies the implemented causal accounting: immutable grants,
reservations, and terminal extinguishment, with Store-transaction arbitration.

The producer of an effect does not unilaterally validate its own work. Acceptance
policy and independent verification are distinct effects/actors where risk
requires separation.

## Consequences

Positive:

- one commitment boundary for every actor and transport;
- replayable rejection and acceptance evidence;
- simpler active-active and backup runtime semantics;
- plugins can evolve without adding feature branches to the kernel;
- task stages can be tested independently before end-to-end composition.

Costs:

- actor key lifecycle and policy evolution become explicit protocol concerns;
- existing RuntimeIngress and Method APIs require staged migration or deletion;
- adapters must preserve atomic append, uniqueness, and reconstruction rules;
- external clients must sign Candidates rather than rely on server-side trust.

## Boundaries

This ADR does not define a token market, worker accounts, consensus protocol,
remote discovery system, or general workflow DSL. Economic analogies remain
internal design lenses. The public protocol exposes allowance, authority,
effects, and evidence—not financial metaphors.

## Migration and verification

`DESIGN.md` remains the current implemented truth.
`changes/001-candidate-genesis.md` owns migration order and deletion criteria.
Architecture tests must enforce at least:

- canonical and deeply immutable Candidate/Genesis values;
- fail-closed signature, domain, key, and capability checks;
- one commitment coordinator without effect-specific branches;
- visible rejection and independent Candidate receipt;
- producer-separated exact-Asset attestation and qualified-output projection;
- Memory/SQLite conformance, reconstruction, concurrent idempotency, and crash
  atomicity;
- monotonically decreasing direct RuntimeIngress and Method exceptions.

The ADR is fully realized only when the active change closes and its result is
folded into `DESIGN.md`.
