# Aigineering Design

Status: implemented development snapshot; integrated behavior and release
qualification remain evidence-dependent.

Aigineering is a zero-trust runtime for auditable AI-assisted engineering and
research. The supported deployment is a bounded single-machine workflow.
SQLite stores signed facts and claim state. Redis is required application
infrastructure for discovery, event feeds and Worker wakeups.

## Implemented runtime path

Every state-changing action follows one path:

```text
intent → typed Candidate → signature and current-authority checks
       → pure projection → reference, scope and funding checks
       → one SQLite transaction → immutable facts and derived views
```

The transaction commits the Candidate's accepted facts, trace, idempotency and
claim transition together. A rejected or interrupted publication does not
leave a partial effect set. Genesis bootstrap is the only setup path before
Candidate admission. Store transaction methods are trusted internal
interfaces, not public ways to bypass signed admission.

Replay verifies and returns the original accepted publication using its exact
receipt, head and effect membership under current key authorization. Replay is
read-only; it never calls a maker to reconstruct effects or writes a replay
marker. Historical facts remain immutable. Rebuild reconstructs read views and
does not repair or publish state.

The current Store reader floor is schema 24. Stop older Workers and back up
before upgrading. This prevents an older process from opening an upgraded
Store; it cannot fence an old process that already has a connection open.

## Actors, Contracts and causal funding

Humans, scripts, Workers and application adapters act through signing keys and
capabilities. The SQLite writer rechecks the current effective key,
capability, Worker registration and claim command. A claimed Worker cannot
widen its Contract's outputs, protected-name authority, inputs, labels, tools,
routing scope or allowance.

Contracts freeze task intent: exact input bindings, output requirements,
execution routing, tool permissions, acceptance policy and causal allowance.
Capability labels select eligible Workers; they are not model identities or
cost classes. Skills and labels resolve context but do not grant authority.

Child publications reserve actual allowance from a valid causal funder in the
same Candidate transaction. The allowance is not a mutable Worker balance or
token bill. A declaration does not itself pay for later work. A root recovery
cannot mint a new grant; when no valid funded parent exists, no recovery
publication is committed. Root retry also remains rejected without a causal
parent funder, preserving the source funding obligation. Integrated validation
is tracked separately from release qualification.

## Assets and task completion

Assets are immutable outputs admitted through signed effects. Content identity,
Definition identity and their signed assertion are distinct. WorkerHost may
reuse a frozen required Definition. A published producer is evidenced by its
signed assertion and publication; a Definition's source metadata does not
establish who produced an Asset.

Ordinary output matching checks the declared name and frozen selectors such as
Definition, content, producer actor and output shape. Any admitted Asset may
meet those conditions, including one published by another Task or an operator.
There is no root-tree or `origin` gate on generic completion. A root is a view
of its own requirements, not a separate execution graph owner. Existing
historical `output_source` metadata is readable but does not impose a hidden
source rule.

Independent acceptance is separate from ordinary output matching. A frozen
policy may require canonical qualification for an exact output. Only the
canonical signed `asset.attest` effect produces qualification facts. A review
report or descriptive relation Asset is not an attestation, though an ordinary
Asset can meet an explicitly matching output requirement.

Tool observations remain local evidence. They do not satisfy a business output
unless a Worker publishes a separate admitted Asset that meets the frozen
conditions.

## Workers, planning and tools

Workers receive a claim-bound package containing the Contract, exact selected
inputs and authorized context. They return one signed Candidate through the
normal WorkerHost path. An existing Harness can use the same signed claim,
renewal and submission adapter; its private key stays outside the runtime.

Planning, delegation, tool work, retry and recovery use ordinary bounded
Contracts. For a single-tool action, the Worker Candidate declares both the
tool task and its continuation. Parallel tool actions use the same scope and
funding rules. Scope is permission, not an obligation to call every available
tool. There is no post-completion scheduler/controller. Read-only projection cannot
create work. Authorized recovery adapters publish ordinary signed Candidates
through the same admission path.

Planning adapters compile bounded child declarations from frozen parent scope.
They do not write an example-owned task graph or private conversation state.
An unmet obligation without eligible funded work remains visible as a blocker.

## Failure and task views

Claim, attempt, invocation failure, cancellation and accepted output are
distinct facts. A failed invocation is evidence about an attempt; it does not
by itself assert that the business Contract is permanently failed. Retry and
recovery publish new work only when a real causal allowance is available.
Deterministic and installed-artifact checks cover bounded failure and recovery
cases. General provider reliability remains a separate qualification goal.

Task status, board, audit, lineage, diagnostics and logs are read projections.
They do not authorize claims, change output eligibility, qualify Assets or
repair history. Pending independent acceptance is reported separately from
missing recovery. Tool, provider and local invocation observations are
auxiliary evidence, not signed business facts.

## Review and application surfaces

Review request compiles to an ordinary Task declaration and descriptive Asset
in one Candidate. Review decision publishes canonical `asset.attest` and a
descriptive decision Asset in one Candidate. Reports and targets are ordinary
admitted Assets checked by frozen identity/shape selectors and producer
separation. There is no task-origin, claim or descendant-tree gate.

The maintained CLI supports ordinary task creation, execution, claim
inspection, Asset publication and attestation, cancellation, retry, review,
export and diagnostics. Some previous control-plane writers (handoff,
revision/edit, mutable assignment, dependencies, holds/promote, priority,
comments and attachment links) are no longer fresh publication paths. Their
historical records may be inspected where supported, but they do not affect
current claim or output eligibility. Worker profile selection uses ordinary
registration.

The existing provider, model, Fleet and workspace defaults are configured by
their current application settings; this design change does not define new
defaults. Redis outages remain visible. SQLite-accepted facts are not discarded
when Redis synchronization is pending.

## Known transition boundaries

The schema 24 reader floor and ordinary output selectors are implemented.
Bounded failure, expired-claim and recovery cases passed integrated checks;
broad real-provider reliability remains unestablished. Historical control
records remain inspectable without acting as fresh writers or current
eligibility rules.

## Release limits

The runtime authenticates assertions and enforces declared policy; it does not
prove universal truth, semantic quality, lower cost, process isolation or
exactly-once external effects. Unknown provider usage is not zero. A deterministic
test or fixture-backed case does not establish real-provider reliability.
See [support and limits](docs/reference/development-status.md), the
[boundary invariants](docs/boundary-invariants.md), and
[change 137](changes/137-closed-primitive-task-runtime.md) for current scope.
