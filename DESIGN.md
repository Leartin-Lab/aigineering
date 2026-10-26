# Aigineering Design

Status: implemented truth for v0.5.13.dev0

This document describes the v0.5.13.dev0 implementation in this source tree.
The configured-run entry is a development addition; v0.5.12 live release evidence
is bound to its earlier snapshot, not to this development tree.
Runtime release-preparation gates passed for local verification; this is not a
published release. See the
[stabilization report](reports/070-usable-cli-stabilization-2026-10-03.md).
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

An existing agent harness may remain the execution environment. The public
HarnessCandidateAdapter compiles its single action through the same WorkerHost
effect path and signs claim, renewal, and result Candidates without exposing
the harness private key to the runtime.

Provider adapters may remove a complete reasoning-only wrapper or serialize a
JSON-valued output as canonical text before action parsing. This is wire-shape
normalization only: the result must still pass the same strict action,
authority, projection, and commitment boundaries.

The OpenAI-compatible adapter classifies a completion marked
`finish_reason="length"` as `output_truncated`, including when the partial
content parses as an action. Empty or whitespace-only message content is
classified as `empty_response`; neither case treats reasoning-only fields as
executable output. Validated provider usage follows the durable Worker failure
path when available.

Provider retry logging uses classified status and attempt metadata. A
non-retryable failure logs its numeric status without formatting the provider
exception body; callers still receive the original exception. This limits that
logger's exposure without sanitizing arbitrary application logs or changing
failure and recovery semantics. See [change 059](changes/059-provider-log-privacy.md).

The action adapter may also unwrap one syntactically valid method action that a
provider accidentally quoted as the sole `/exec` key/value pair. Ambiguous,
partial, or unsupported wrappers remain rejected. For a planned, retry, or
recovery task with unresolved tool scope, the Contract prompt permits only
tool dispatch; output publication happens in the observation continuation.

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

Authentication rejection persists only its rejection audit and trace. Claimed
actor, Candidate ID and claim binding remain untrusted metadata: they cannot
close an attempt, submit a claim, terminate a task, extinguish allowance or
schedule recovery. The audit path does not use a claimed Candidate ID to skip
rejection as an already committed replay. Authenticated output rejection retains
its receipt-backed attempt and recovery behavior. Previously committed incorrect
facts remain immutable; audit and rebuild do not repair them. Stop older Worker
processes before using the repaired ingress; there is no new reader-floor gate
that constrains an already-open older writer. See
[authentication rejection isolation](changes/057-authentication-rejection-isolation.md).

Store transaction primitives are trusted implementation interfaces, not external
publication APIs. Raw record append refuses balance-changing allowance facts;
signed ingress checks their canonical declaration and funding provenance again
inside commitment. This does not provide process isolation or cryptographic
revalidation of arbitrary local Python calls or coordinated database rewrites.

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
Canonical identity hashing and signing interfaces are owned by
`aigineering.protocol`; the former `aigineering.core.ids` and
`aigineering.core.signing` paths are compatibility re-exports. Protocol modules
do not depend back on runtime-core implementations.
Signed JSON uses a language-neutral canonical subset:

- string object keys;
- object keys are already NFC and unique after normalization;
- Candidate causal parents are arrays of non-empty strings;
- no floats, NaN, Infinity, bytes, sets, or unsafe integers;
- stable ordering and serialization.

Contract identity binds every field that changes execution authority, including
parent, inputs, outputs, activation, allowance declaration, tools, labels,
Worker requirements, origin, minting authority, sensitive-input policy, and
acceptance policy. Current v4 Contracts also bind the exact Asset IDs resolved
from label syntax at construction time. Replay and recursive task publication
reuse those IDs instead of consulting a changed label catalog.

Current v5 Contracts additionally separate execution requirements from
delegation scope. `worker_capabilities` and `worker_pools` select who may execute
the Contract. `delegation_capabilities` and `delegation_pools` bound what its
descendants may require. These are capability labels, not model names or cost
classes; operator-owned Worker registrations map them to concrete providers.

Contracts with `sensitive_input_policy.input_asset_bindings` use v6. It reuses
the complete v5 canonical field owner while marking exact input-selection support.
Guard-bearing older identities are rejected; historical no-key identities stay
unchanged. This reader gate began with schema18; the current floor is schema21,
which also protects paired causal-allowance returns, task claim constraints and
Worker assignments. It prevents older readers
from reopening an upgraded Store,
without changing historical facts or adding columns. Stop old Workers and use
`domain upgrade` to back up before migration; ordinary writable opens also migrate.
The gate cannot protect an already-open old connection. See
[ADR-032](docs/adr/ADR-032-exact-input-binding-reader-gates.md).

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

Exact derived evidence is stronger than an advisory relation. A slice relation
binds its source, replacement, lineage, range, and derivation version. Runtime
policy and verification recompute the exact line, character, or UTF-8 byte
range from the committed source; the relation signature alone cannot make
caller-supplied content a valid slice.

## Portable business artifacts

The optional `business` adapter package implements `artifact-v1` envelopes over
ordinary Assets. Bounded base64 attachments preserve byte SHA-256 separately
from normalized text identity. Document, exact page/character evidence and
Markdown citation bindings refer to exact accepted Asset IDs. These conventions
also fit canonical Worker text outputs; they add no kernel effect or state.

The `artifact` CLI signs administrative ingress, validates recursive business
closure and exports Markdown, escaped evidence pages and ancestry metadata.
Scoped ToolRegistry handlers return observations only. Consumer validation
checks byte integrity and exact quotes; semantic support and parser fidelity
remain unproven. Attachments are limited to 8 MiB, sources are omitted from
exports unless explicitly included, and non-original disclosure views fail
closed at every ancestry hop. See ADR-022 and `docs/business-artifacts.md`.

## Declarative methods

The `methods` application adapter packages procedures, exact dependencies, input
slots, output shapes and routing/tool requirements as ordinary signed Assets.
Instantiation creates normal Contracts with frozen input Asset bindings and an
explicit tool grant. A seed authoring method produces candidate packages using
the same Worker protocol; it owns no private continuation state or authority.

Separate test suites compile to ordinary case Contracts. Expected values are
withheld from case Workers and read by a deterministic evaluation Worker. Default
reuse requires an exact passing report, an independent policy-bound attestation,
and consumer reconstruction of the test evidence. Explicit unverified trials are
recorded in the invocation Contract. Import, evaluation and execution permission
remain distinct. See ADR-023 and `docs/methods.md` for limits.

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
possession of the registered Worker key. Claims and renewals also carry their
original signed coordination Candidate into the SQLite writer transaction,
which rechecks current key authorization, `worker.submit`, Worker registration,
and claim binding before mutation. Claim selection also fences the registration
version used for package preparation. A signed grant links to its request fact;
a signed renewal links to both its grant and renewal request, and each request
links to the Candidate receipt. Successful requests are single-use, while a
no-work null-target scan records no request and can be retried. Local WorkerHost,
Fleet, Engine, method, CLI, harness and HTTP paths share this authentication
boundary. Recordless trusted Store compatibility calls are internal seams, not
operational authentication evidence. Coordination itself did not change fact
types or the reader floor; task assignments now require schema21. Expired claims, invocation failures,
malformed Worker results, and Contract terminal facts close visibly. A terminal
fact releases an unrelated active claim in the same transaction and records an
immutable transition that rebuilds to the same claim view. A stale Candidate
cannot publish after terminal closure; exact replay of an already committed
Candidate remains idempotent. Recovery is published only when its Candidate is
accepted.

Local Worker setup shares one registration service. A canonical local pull-only
registration may advance from an unset profile to a concrete adapter profile
through a signed, atomic promotion/register pair, preserving its Worker ID and
key. The plugin owns only the promotion audit; current head, identity and absence
of active claims are rechecked by the generic injected-effect validator inside
commitment. Local hosts freeze their resolved version through claim selection
and the writer fence. Exact committed promotion history permits repeated default
launches without new facts. Ordinary explicit registration and external signed
command shapes remain unchanged. Profile is descriptive metadata; stop older
local processes before promotion. See
[ADR-039](docs/adr/ADR-039-local-worker-profile-promotion.md).

The SQLite submission transaction rechecks the claim fence and commits the
Candidate, projection, accepted facts, trace, idempotency record, attempt
outcome, allowance consequences, and claim transition together.

Task priority is a plugin-owned signed advisory history. Its exclusive
`task.priority.changed` facts carry an exact predecessor, actor, key and Candidate;
the writer rechecks the chain. Zero clears the hint while retaining the head.
One application service orders the supplied candidate IDs by descending priority,
declaration revision and full ID. Broad pulls and scoped application enumerations
share that service; Runtime's generic permutation hook cannot filter tasks or
grant eligibility, and explicit task claims remain pinned. Current claims,
assignments, readiness, holds, prerequisites and SQLite arbitration remain
authoritative. Views expose the hint, but successors do not inherit it. Ordering
is local to a scan snapshot, with no global fairness or mixed-version guarantee.
The advisory hint changes no Contract identity or reader floor. See
[ADR-041](docs/adr/ADR-041-advisory-task-priority.md).

Priority validation and publication share the same target lookup guard. A
genuinely absent Contract with no exact declaration or priority history is
not found. Orphan typed history, ambiguous materialization and malformed
priority history remain integrity failures; bulk collection stops on unknown
failures rather than treating them as missing tasks. Authentication audit's
unverified claimed IDs do not select task history. See
[change 081](changes/081-bulk-priority-missing-target.md).

See [ADR-037](docs/adr/ADR-037-transactional-worker-authentication.md) for the
operational coordination authentication and writer-fencing decision. ADR-012
records its original HTTP-only scope and supersession history.

Pure output reduction normally runs before the write transaction. SQLite runs
the same reducer again after acquiring its writer lock and inserting the batch
Assets, then commits only newly visible consequences. Two concurrent Candidates
that satisfy different outputs therefore converge on exactly one terminal fact
instead of leaving an output-complete Contract unterminated.

Pending Asset batches use the shared output policy with actual Asset values,
retaining every same-name value. Names alone remain activation inputs, while
business-output origin, system/recovery producer and exact independent
qualification rules still apply to completion and descendant fences. Existing
terminal facts remain immutable; see
[change125](changes/125-pending-asset-output-policy.md).

Nonempty Asset reduction builds an immutable invocation-local multivalue dispatch
index for exact method creators, raw activation tokens and output names. Candidate
buckets preserve original order while descendant traversal retains the full
Contract overlay. A deque preserves FIFO cancellation without front-list shifts.
This does not remove full-history capture, output-policy or ancestor work; see
[change126](changes/126-batch-asset-dispatch.md).

SQLite can select a bounded immutable context for homogeneous built-in Contract
declarations. For supported canonical task identities, the allowance owner consumes
raw per-source totals while the original commitment transaction still performs
two fresh validations. Optional SQLite allowance checkpoints and publication
head-member relations are rebuildable projections, maintained with their source
facts in the same transaction. Old-writer tails are read within the selected
snapshot; readers and audits never advance cache state. Missing or known-invalid
cache data uses the original reference validation path; unknown storage failures
propagate. No task, budget, proof or acceptance authority moves into the indexes.
Cold build, complete audit, rebuild and unsupported history remain explicit costs;
Asset and claim paths are separate. See [ADR-047](docs/adr/ADR-047-rebuildable-allowance-checkpoints.md)
and [ADR-048](docs/adr/ADR-048-rebuildable-publication-relations.md).

For homogeneous built-in Asset proposals, an optional Store reader can instead
select only the exact claimed parent and the complete single-effect Candidate
replay witnesses. Unbound multi-effect proposals require no historical
projection context. Mixed, graph, injected and uncertain shapes retain the full
loader. Selection is read-only; the original replay proof and writer-time
claim/key/output checks still decide. This does not localize the separate
pre-commit or writer Asset consequence reducer. See
[change 123](changes/123-scoped-asset-proposal-context.md).

## Disclosure

A Worker receives a frozen package containing the exact disclosed Asset IDs and
content views for that attempt. Disclosure is restricted to declared inputs and
promptable label-referenced Assets, then filtered by redaction and sensitive
input policy.

Mapped input roles select exact sorted unique IDs through one shared owner used
by disclosure and verification. Active missing, wrong-role and tombstoned references
fail closed without name fallback; empty arrays select nothing. Unmapped roles keep
authorized name selection and unused inherited bindings are inert. Original signer,
trust, derivation and redaction checks still apply; a binding grants no new input
role or capability. Legacy unbound tasks are not retroactively frozen.

Disclosure and method-context assembly can use the optional Store batch Asset
read for frozen IDs. SQLite bounds each `IN` query by its variable limit;
readers without the optional method retain ordered per-ID lookup. The required
Store protocol is unchanged.

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
Reserved runtime namespaces require the current Candidate actor's protected
publication capability or exact inherited minting authority. This applies to
new signed graph definitions and Assets materialized by new graph assertions,
including assertions that reuse a historical definition. Tools and observations
cannot satisfy business outputs merely because their names appear in the Store.
The guard affects new projections; committed historical facts are not rewritten
by rebuild. Stop older Worker processes before relying on the repaired boundary.

## Plugins and recursive work

Plugins are Store-free proposal functions. They receive a frozen request and
return ordinary Candidate effects. They do not receive an Engine or mutation
handle.

All new runtime-created Contracts use one canonical construction helper so the
materialized immutable entity and its selected v3, v4, v5, or v6 identity cannot
drift across planning, retry, recovery, continuation, CLI, or nested-Worker paths.

Planning and replanning publish three ordinary tasks in one atomic group:

```text
draft
→ dependency analysis
→ compile
→ ordinary child Contracts
```

When allowance permits, the staged expansion leaves up to three units at the
parent for one durable repair of each stage. The reserve remains causal
allowance, not an in-memory retry counter.

Each stage is independently claimable and testable. Compile enforces:

- non-empty executable descriptions and outputs;
- monotonic activation syntax;
- input reachability from disclosed facts or accepted producers;
- complete parent-output recommitment;
- at least two allowance units for a task with tool scope, covering one tool
  task and its continuation;
- labels contained within the parent's non-Plugin context-selection scope;
- tool, Worker, authority, and allowance containment.

An authorized compile task may use ordinary tools before returning its
blueprint. New claim-bound method children exclude the six planning-stage
control labels; a new nested stage carries only its current stage label. Prompt
and Worker compilation share one pure active-stage resolver. Explicit typed
system method roles, including their retry and recovery forms, take precedence
over legacy inherited stage labels. Genuine compile continuations and repairs
retain the blueprint fence, including ambiguous legacy compile metadata.
Historical method-source proof recognizes exact old and current publications
without changing their Contracts, scope or allowance. See
[change 114](changes/114-isolate-planning-stage-roles-from-method-children.md).

Both planning formats default an omitted child budget to two for nonempty tool
scope and one otherwise. Explicit insufficient tool budgets remain rejected;
containment cannot reduce a tool child below that minimum. This is a minimum
for one tool and continuation, not a cost formula for arbitrary tool chains.
See [change 107](changes/107-planned-tool-budget-default.md).

A final reachability closure is computed from accepted producers only. A
rejected producer, self-dependency, or ungrounded cycle cannot leave an admitted
task permanently disabled.

Replanning uses the same task protocol recursively. Tool use, fail, retry,
continuation, recovery, and verification also publish or complete ordinary
facts through registered Plugins.

A successful tool task publishes a continuation whose scope omits that exact
tool. Repeated calls are represented as separate ordinary tasks rather than an
unbounded same-task tool loop; other parent-authorized tools remain available.
New continuations retain the parent's input roles and exact input policy. Their
method context freezes the ordered, deduplicated union of the exact parent's
previous context and the current tool observations before publication. An
interrupted publication reuses those prepared IDs. Historical recognition
accepts the original legacy empty-input construction but never replaces its
Contract or refreshes its saved context; conflicting history fails closed. See
[change 110](changes/110-continuation-input-and-observation-retention.md).
Tool and synchronous MCP adapter preflight or invocation failures raise the
existing `WorkerExecutionError` with a fixed safe code and message, so no
observation Candidate is produced and the ordinary Worker failure path closes
the claim and reports the safe cause through task runs and claim-bound
diagnostics. Recovery, when available, is new ordinary work and may not have
an executable target. Successful tool results remain observations,
including business-level negative JSON returned as a string. Already committed
legacy `ok:false` observations remain immutable local evidence: a live parent
may continue through ordinary work using them as context, without treating them
as tool success or qualifying business output. Completion preserves an
existing typed parent terminal and publishes no new continuation for it.

Projection and method-result repair publish new recovery Contracts and retain
the failed task's Skill context, routing scope, tools, and acceptance policy.
Recovery depth is bounded at three immutable successor attempts. A recovery
with unresolved tool scope cannot publish tool-derived outputs directly; it
must first publish a tool task and consume the committed observation.
When a claim-proven system tool method fails, its bounded recovery successor
retains the exact tool method, arguments, input scope, output roles, capabilities
and acceptance policy. A normal signed Worker claim and ToolWorker execution
can therefore retry the same authorized call, followed by the existing method
completion and continuation owners. Invocation failure, authenticated claim
expiration and admitted Candidate rejection use their existing exact claim
proofs; an unverified historical rejection cannot publish successor work.
Ordinary business-task repair retains its existing generic recovery shape.
After an interruption, recovery recognizes an existing publication only when
the original signed Candidate intent, receipt, committed head, canonical
Contract/context projections, and current materializations match. This
read-only proof can reconcile an accepted publication after its signing key is
revoked; any new publication still uses current Candidate authentication.
Publication and the later recovery outcome remain separate writes.
Recovery declaration comparisons use canonical wire containers, preserving
exact keys, values and sequence order while normalizing frozen tuples and JSON
lists. All receipt, head/member and materialization proofs still apply. See
[change 111](changes/111-recovery-binding-wire-comparison.md).

`/parallel_tool` compiles two to eight calls into independently claimable tool
Contracts and one continuation activated by the boolean conjunction of their
observation names. Tool-specific capabilities route each call; the continuation
cannot run until every observation is committed. When the signed allowance has
room beyond one unit per call and the one-unit join minimum, publication leaves
up to one unreserved parent unit per item for a first recovery publication. The
remaining allowance still determines the join budget. This is ordinary
available balance, not an earmark or a guarantee if other authorized work spends
it first; recursive recovery and failures beyond the retained capacity can remain
unavailable. At the minimum `calls + 1` allowance, no recovery unit is retained.
No thread, future, or in-memory join is part of task truth.

For an eligible business Contract with independent acceptance, publishing a
tool continuation retains up to one unit of the parent's ordinary allowance per
declared output, subject to keeping at least one unit for the continuation. The
continuation receives the remaining allowance. Non-eligible or non-independent
parents keep their existing allocation, and a continuation-origin parent does
not reserve the same review demand again. Parallel tool publication uses the
same allocation owner and combines review demand with its bounded first-recovery
demand; any remaining parent balance is shared ordinary allowance, not a
priority or earmark. These reserves fund the existing default one-unit review
request per output when enough allowance remains; they do not guarantee a larger
review budget, verifier availability, or acceptance. Completion fixes a tool
continuation's intended budget from the exact accepted declaration head of its
source Contract, including that declaration's allowance batch. After restart, an
already accepted child is reused only when its exact Candidate intent, receipt,
canonical Contract, full committed batch and configured publisher actor/domain
match; recovery does not sign it again. If no child exists, the fixed intent goes
through the current signed publisher. A later authorized spend can make that
intent unaffordable, in which case the writer rejects it visibly instead of
reducing its budget. Completion does not clear an existing failed parent
terminal or republish work beneath it.

## Causal allowance

Allowance is lineage authority, not a Worker wallet or mutable counter.

- a root declaration creates an immutable grant;
- child publication reserves from its causal parent and grants the child;
- terminal projection extinguishes the positive integer remainder once, paired
  to the Contract's unique typed terminal fact by exact causal parent and
  terminal reason;
- exact Candidate replay cannot reserve twice.

A newly declared root Contract with `origin="retry"` cannot receive an unfunded
root grant. Pure projection rejects it, and commit-time validation independently
rejects a new grant of that shape after checking canonical provenance. The
rejection retains audit facts without publishing a successor or allowance.
Contained CLI retries retain their existing reservation from the common
containment parent; Worker `/retry` continues to reserve from the claimed source.
Historical accepted root retries remain readable and exactly replayable without
new grants. This refusal does not choose a transfer, cancellation, freezing or
work-authorization policy for retries. See
[change 104](changes/104-retry-root-grant-conservation.md).

Pure projection rejects an oversized batch. SQLite repeats the balance check in
the commitment transaction against the complete pending Candidate and derived
fact batch so concurrent publishers cannot overspend the same grant or cause a
valid terminal consequence to omit a same-batch reservation. Exact replay adds
no second debit; validating an unrelated new batch does not rewrite historical
facts.

The same allowance owner materializes terminal consequences for Candidate
ingress, Worker invocation failure, and claim expiration. SQLite commits a claim
transition, its terminal fact, and any remaining-allowance extinguishment in one
writer transaction. A new reservation cannot use a source Contract that already
has a terminal fact; exact replay does not backfill missing historical burns.

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

An independent policy may additionally bind deterministic `output_shapes`.
The small language supports exact JSON objects, non-empty arrays, strings,
non-empty strings, finite numbers, and booleans. Shape-parsed JSON rejects
non-standard numeric constants and exponent overflow; an output without a
shape remains ordinary text. Shape validation runs when the producer submits
an output and again before qualification. Planning, recovery, retry, and
continuation inherit the relevant shapes, so a verifier opinion cannot
legalize mechanically malformed content.

The exact Asset may be produced by the Contract itself or by a Contract whose
immutable parent chain reaches it. This supports plan and tool descendants
without accepting an unrelated same-name Asset.

## Persistence and reconstruction

RuntimeRecord is the append-only replay envelope. SQLite materializes Contracts,
Assets, trace entries, claims, Worker registrations, idempotency, replacement
claims, allowance, and terminal views for efficient access.

Materialized views are disposable. Rebuild:

1. reads immutable runtime records;
2. reconstructs materializations;
3. computes a semantic digest;
4. verifies it matches the pre-rebuild digest.

Worker-registration verification includes its actor and key binding alongside
routing fields. Display timestamps remain excluded because live writes and
fact-derived reconstruction can assign different values. This digest checks
local consistency; it does not establish signature authenticity or log completeness.

Historical SQLite schemas are migrated explicitly and tested. Missing causal
evidence, conflicting records, or unrecorded rows fail reconstruction rather
than being silently ignored.

## Concurrency and restart

Concurrency correctness belongs to Store transactions and uniqueness
constraints, not process-local locks.

Multiple same-machine Worker processes may use separate SQLite connections over
one WAL database. Claim epochs fence stale results. A replacement process can
reconstruct task progress and continue without restoring an Engine snapshot.

CLI, local Fleet, and nested Workers share one stateless maintenance step for
durable Worker failures, rejected submissions, and task-completion projection.
Target-pool cycle summaries scan the bounded RuntimeRecord revision window and
resolve trace ownership through typed Contract references, including
commitment traces joined through committed records. They list unproven trace
IDs separately and preserve cycles that contain events but no provable
Contract. The surrounding loops own only polling and termination policy; they
do not own task truth.

When a provider has returned well-formed usage, a later response-extraction,
tool-mapping, or Candidate-envelope failure retains that usage in the same
durable failure trace and RuntimeRecord. The failure path applies an explicit
usage allowlist and does not synthesize zero usage when the provider reports
none.

Plan and replan compile failures can additionally retain bounded, versioned
structured rejection diagnostics. One pure application format owner validates
both failure writes and exact claim-history reads. The generic recovery keeps
its readable summary and includes these details in its existing failure-context
Asset; neither diagnostic recoverability nor a published successor proves repair
success. Legacy failures keep their original summary. Writer, reader and consumer
deployment must stay aligned because the optional durable field changes recovery
intent reconstruction. See
[structured compile diagnostics](changes/105-structured-compile-failure-diagnostics.md).

Usage metadata has one protocol-owned allowlist and validator. Local LLM and
tool adapters filter unknown or malformed fields before building a signed
CandidateEnvelope; direct envelope and harness inputs are validated strictly.
Signed Candidates with invalid usage are rejected without rewriting their
signed payload, and a sanitized receipt preserves observable claim closure and
recovery. Invalid usage cannot suppress terminal failure evidence. Counts are
non-boolean integers from zero through `2^53-1`; strings are bounded UTF-8.
Raw metadata and usage records preserve absent usage separately from explicit
zero; identity-only LLM metadata does not create a usage record when no usage
amount is present. Productivity token totals aggregate known counts: an empty
aggregate is zero, and a total may be derived by adding known prompt and
completion counts. Neither an empty aggregate nor a zero total proves that
the provider charged nothing. Usage reports are not a complete billing ledger.

Continuation method-context lookup reads trace events by their
`relation_target`, then applies the existing event-type, asset, ordering, and
disclosure rules. SQLite initializes an ordinary relation-target index when a
store opens, including an existing database; this does not add a fact or change
the schema version. Operational Store adapters provide the same targeted trace
read capability.

Quick-run session manifests describe the selected root Contract and its
descendants. Their evidence Asset IDs come from frozen context bindings and
exact Asset IDs referenced by tree traces, not from looking up every Asset
with a matching input name. `produced_asset_ids` separately identifies Assets
committed during the run's revision window. The manifest records the
exclusive start and inclusive end RuntimeRecord revisions; session JSONL
exports only trace records from that bounded window whose Contract ownership
is proven by typed record references. Commitment traces without a claim
binding are attributed through their committed record links, never by treating
the `commitment` label as a Contract. Unattributed commitment trace IDs remain
explicitly unassigned. Old manifests load as `legacy_unscoped` because their
former whole-store membership cannot be recovered from the file alone.

SQLite runtime-record scans accept an optional inclusive upper revision bound.
`SQLiteStore.snapshot()` copies a live connection through SQLite backup;
`snapshot_from_path()` opens source files read-only and rejects schema
mismatches without applying a migration. Newly written trace JSON uses stable
key ordering in SQLite rows and JSONL exports. Existing stored rows are not
rewritten, and these changes do not alter the SQLite schema version.

The local Fleet launcher applies the same protocol within one process. Each
capacity slot owns an independent SQLite connection and repeats only the normal
pull, claim, invoke, and submit operations. It does not own a task queue or
scheduling state. TOML profiles bind Worker adapters to capabilities, pools,
capacity, and secret environment-variable names; task semantics remain model
independent.

Bounded and persistent Fleet slots enrich a caught invocation failure only
through the shared exact-claim diagnostics owner. Proven result details carry
the task, claim, package, Worker and failure/release fact references at one
fixed Store revision; unavailable proof carries no claimed identifiers.
Coordinator exceptions discovered after joining bounded slots are reported as
unbound safe errors. Neither path creates task state or exposes exception text.

EngineWorker applies the same rule across nested fact domains. It receives an
outer claim context, executes against an isolated inner Store, persists bridge
operations, and exports only authorized output effects. A restarted
EngineWorker can reopen the inner Store and reuse committed work; an expired
outer claim cannot submit a late result.

## Query and interface surfaces

CLI and optional HTTP endpoints are adapters over the same Candidate and Worker
protocols. They do not own alternate mutation semantics.

Named local boards are application routing configuration. `local_boards.py`
owns a project-local registry of canonical workspace paths, exact Genesis pins,
archive flags and saved selection. Global `--board` selects one invocation;
explicit `-C` bypasses selection. Missing, replaced, corrupt or archived selected
domains fail visibly. Management remains at the registry root and cwd is
restored on exit. Archive changes metadata only; it neither cancels work nor
stops Workers. `local_domain.py` shares explicit initialization between
`domain init` and managed board creation, preserving the core Genesis owner.
Atomic locked registry replacement and retry of retained post-Genesis setup
avoid a separate task/state owner. Registry data grants no capability, scope or
acceptance and is not OS isolation. See
[ADR-046](docs/adr/ADR-046-named-board-workspace-routing.md).

Redis is a required application collaboration component. Normal CLI operations
and HTTP Store requests preflight it; application read queries are strict Redis
views. The default address is local Redis, configurable through
`AIGINEERING_REDIS_URL`. Explicit offline CLI inspection remains available for
selected SQLite audit views. Redis is installed as a base dependency.

```text
SQLite committed RuntimeRecords (replayable source)
→ domain/schema-scoped Redis event stream and read indexes
→ task discovery, event cursors, bounded Worker wakeups and CLI/API views
→ ordinary SQLite pull/claim/commit boundary
```

An application relay publishes only record metadata and typed task references to
the event stream. Before synchronization it proves the complete Redis prefix
against the caller's SQLite snapshot while watching the stream and metadata keys.
Small suffixes append with one watched transaction; larger suffixes stage a full
snapshot in bounded chunks whose TTL is committed with each page, then publish it
with one prevalidated Lua swap that checks the exact staged event count. Ordinary
synchronization never clears or repairs a mismatched projection. The explicit
`aig redis rebuild` command watches the live fence before opening a fresh snapshot
from its fixed routed Store path. Only its first watched attempt may repair the
captured damaged or ahead projection; a retry must prove the newer live state as
a prefix of a fresh routed snapshot. An older snapshot reports a newer Redis
watermark as noncurrent; its event view remains bounded to the exact source-known
prefix. Empty history is represented by both live Redis keys being absent. Wait
operations use XREAD as a wake hint and poll the caller's source revision, so a
committed suffix becomes observable even without a new wake event; empty polls
do not rescan the full history. Query-index generation publication has a separate
owner; collaboration-prefix validation does not validate query-index contents.
Connection and corruption failures are visible in strict application reads;
low-level compatibility fallback requires explicit selection.

Redis is independently rebuildable infrastructure, not a second Store authority.
Commitment, authority, allowance, acceptance, claims, fencing, idempotency and
terminal decisions never import or query it. SQLite commitment and Redis relay
are separate failure domains: post-commit sync failure reports retained facts and
pending synchronization. Default execution requires the service to be healthy.

The assignment and priority application owners construct their exact durable
operation result before post-commit relay. Their mutation CLI reports the
retained Candidate, direct fact IDs, actual idempotency key and observed revision
on synchronization pending. Recovery repeats the original intent and predecessor
with that key. Pre-publication failures carry no operation result; rejected
Candidates carry only their exact audit result. Reporting metadata grants no
execution or output authority. See
[change 066](changes/066-assignment-priority-durable-pending-results.md).

Shared application task views serve the task CLI and execution adapters. The CLI
adds list, board, stats, show, exact result export, cancel, retry, events and bounded
watch. Columns are projections of facts, not editable scheduler status. Retry
shares one signed publisher with its compatibility command; labels and scope stay
frozen. Result export checks integrity and exact policy-qualified business Assets.
Canonical submitted claims remain `submitted` while required qualification is
absent. The read view exposes eligible complete assertions as exact
`qualification_pending_outputs` references for still-unqualified slots with an independent-acceptance
blocker; it does not classify that blocker alone as missing recovery. Terminal,
completion and expanded-work precedence remain authoritative. Existing canonical
output/lineage/policy validators own eligibility; the view cannot attest or
reset a claim. Aggregate list, board and stats share one immutable revision-bearing
fact snapshot per read; the view context is bound to its source Store and cannot
be reused across snapshots. This avoids repeated persisted fact scans while
retaining full-history validation. Descendant pending-acceptance risk checks use
that same captured effect projection; the narrow read facade never falls back
to fresh Store fact loading. See
[change 113](changes/113-reuse-task-view-projection-snapshot-for-descendant-risks.md),
[change 061](changes/061-submitted-task-view.md)
and [change 076](changes/076-task-view-query-reuse.md).
Fleet slots claim only the selected immutable task tree, use Redis notifications
with bounded waits, and return to SQLite for every claim and submission decision.
See [ADR-027](docs/adr/ADR-027-required-redis-collaboration.md) and the
[task operations reference](docs/reference/task-board.md).

Redis query generations bind a consistent SQLite Store snapshot to an exact
ordered RuntimeRecord prefix. The adapter hashes revision, record ID and the
canonical effective payload in bounded 256-revision scan windows, retaining only
a zero seed plus the latest 16 actual-row checkpoints and an append tip for the
same open Store. If the live revision falls below the cached append tip, the
memo resets before an out-of-range request is refused; arbitrary in-place Store
mutation remains unsupported. A requested nonzero watermark must also be present
as the terminal row; interior revision gaps remain valid. Snapshot
revision, source revision and prefix certificate must agree; equal revision or
graph digest alone does not identify same-revision histories. The cold path may
scan the committed prefix, while repeated reads on the same Store reuse its exact
append tip or rescan from a nearby checkpoint.

If a concurrent relay has advanced Redis beyond a sync's captured prefix, sync
may recapture from the same configured Store only when its revision advanced
and the entire original prefix is unchanged. It retries the full proof within
the existing bounded attempt budget; genuine ahead history, divergence and
exhausted contention remain visible errors. Event reads keep their own frozen
capture and can retry sync contention only through their existing bounded
prefix-proof loop. They cannot refresh their source or return unproven events.
After a watched transaction raises `WatchError`, sync also recaptures that same
Store when attempts remain. It adopts the new capture only when the revision
strictly advanced and the complete original prefix is preserved; a fork stops
as `source_diverged`. A non-advancing capture leaves the original snapshot in
place. The final failed attempt does not trigger another capture, and repeated
conflicts remain visible as `sync_contention`; sustained concurrent writes can
still exhaust the fixed budget. See
[change 112](changes/112-concurrent-relay-source-refresh.md) and
[change 116](changes/116-refresh-redis-source-after-watch-conflict.md).

Full rebuild watches the active pointer and active/target metadata through
`EXEC`, and enumerates target keys while the target metadata is watched. One
fixed Lua publication marks the target non-ready, removes enumerated old keys
with `DEL` calls grouped to at most 128 keys, writes the replacement payload and
metadata, marks it ready, and changes the active pointer last. Catch-up uses the same non-ready/data/ready
publication fence. Redis scripts are atomic to concurrent commands, but a Lua
command error does not roll back commands already run; if one occurs after the
first metadata write, that target remains non-ready and its prior keys may be
partially changed. Reads refuse non-ready selected metadata. Only an explicit
rebuild supplied with a fresh routed Store may repair it, and a WATCH conflict
consumes that repair privilege before retry. A retry must validate the current
prefix and will refuse divergent metadata. Both publication paths retry at most
three WATCH conflicts.

Each cached read watches the selected pointer and metadata through its
multi-command fetch and validating `EXEC`, then rechecks the exact SQLite prefix
and revision after `EXEC`. This catches SQLite-only commits and same-revision
forks that do not touch Redis. JSON views include the prefix certificate in their
cache key; a cache miss writes the payload and touches watched metadata in the
same `EXEC`, so a writer during target-key enumeration invalidates the cleanup
attempt. The query namespace advances from v2 to v3, isolating selected keys from
old v2 writers. Explicit compatibility fallbacks check source stability around
each read. Full snapshot rebuilds still use SQLite's in-memory backup, so copy
cost grows with Store size; prefix identity adds bounded-window scans on cold
sources. This change does not reduce the known large-store rebuild memory cost.
See [change 103](changes/103-query-source-binding-and-conditional-repair.md).

Task conversations use a dedicated `task.comment` capability. The collaboration
plugin projects an exact Contract, bounded text and optional same-task reply to
an immutable `task.commented` record with authenticated actor/key attribution.
The CLI provisions that actor through signed root authorization and explicitly
injects the frozen plugin registry into the generic Candidate publisher.
Additional handlers cannot override built-ins, and the core has no plugin
imports. Commitment uses the ordinary Candidate boundary. Comments remain allowed after terminal
outcomes and do not alter claims, allowance, Contracts, Assets, output obligations,
or acceptance. Identical keyed Candidates return the original durable record;
changed input remains a new Candidate. See
[ADR-028](docs/adr/ADR-028-signed-task-comments.md).

Task attachment association has one application owner in `task_attachments.py`
and an explicitly injected plugin. A sole signed `task.attachment.link` effect
requires `task.attach`, pins the canonical task declaration and supported
original attachment publication witnesses, and emits the selectively exclusive
`task.attachment.linked` relation. Existing generic commitment rechecks current
actor/key and pure projection under the SQLite writer lock. Historical accepted
receipt/head/member evidence is distinct from re-verifying original signatures
when full original payloads are unavailable.

Relations are append-only operator metadata, including on terminal tasks. They
preserve inputs, context, claims, allowance and output qualification. Safe
paginated history and exact-byte export use a shared Store resolver/read snapshot;
export compares the currently available original Asset with its pinned committed
member, reuses the artifact byte validator, and exclusively creates a private
file. Failure can leave its partial output, never overwrite a prior destination.
Redis remains required; post-commit relay failure reports retained accepted IDs,
and exact add replay after recovery synchronizes the same facts. See
[ADR-045](docs/adr/ADR-045-signed-task-attachment-links.md) and
[task attachments](docs/reference/task-attachments.md).

Operator holds use separately authorized `task.block` and `task.unblock`
single-effect Candidates. The collaboration plugin produces generic immutable
`claim.hold.placed` and `claim.hold.released` facts. Each placement record ID
identifies one hold; any open hold prevents claim acquisition. The core reducer
uses the same revision cutoff as other task facts. SQLite rechecks holds in the
claim transaction and claim history in the hold transaction, so a stale view
cannot allow both placement and claim to win. Competing releases are arbitrated
under the same writer lock. No mutable hold table owns task state.

Placement refuses terminal tasks and tasks with any claim history. Exact replay
returns the original fact after later release, claim or terminal changes.
Authorized domain operators may release another actor's exact hold. Releasing
an old hold after terminal closure does not reopen work. Active Worker handoff
requires a successor workflow; these controls only stop unclaimed work from
starting. The CLI provides readable collaboration output by default and optional
JSON envelopes. See [ADR-029](docs/adr/ADR-029-signed-claim-holds.md).

Operator promotion is a separate sole signed `task.promote` effect requiring
`task.unblock`. It binds the complete sorted observed hold set and exact Contract
declaration, emits ordinary releases plus an exclusively owned `task.promoted`
audit, and rechecks the set and eligibility under the writer lock. Generic hold
validation admits up to 64 homogeneous same-Contract transitions with distinct
records and release targets; mixed and cross-task batches reject. Fresh promotion
refuses terminal, previously claimed and canonically output-satisfied tasks.
Dependencies, activation, assignment, capacity, allowance and acceptance retain
their owners. Results expose original releases and observed remaining blockers.
Exact explicit replay uses the original hold IDs, reason and returned key;
later holds remain effective. Multi-task CLI calls are independently atomic and
report partial failures. Redis remains required. No schema or reader-floor change
is introduced. See [ADR-042](docs/adr/ADR-042-exact-hold-set-promotion.md).

CLI block, cancel and exact unblock also accept multiple targets through shared
application publication owners. A common collector validates the invocation,
derives per-target keys and reports ordered partial results; it does not sign
effects, interpret eligibility or introduce a batch state owner. Every target
remains an independent Candidate transaction. Exact hold mappings allow
multi-task promotion to replay original complete sets after later holds appear.
Cancellation uses the shared task publication owner and preserves its original
single-task reason, default key and causal parent. Post-commit Redis failure
carries the actual decision and exact durable result to the CLI; receipt/head
membership and all record identity fields are checked, excluding only the
non-identity timestamp. No history scan substitutes an unrelated result.
When a signed cancellation is rejected and its durable audit proof verifies, the
single and batch CLIs return that exact rejection result and observed revision.
Only the typed verified rejection is an expected business refusal; an untyped
error or unavailable proof remains an internal/unknown outcome. Authentication
rejection IDs remain claimed metadata, not authenticated Candidate identity.
Human rejection output includes the durable audit record reference. A single
post-commit pending response keeps the operation's prepared revision if later
Store activity advances the current revision. A Redis preflight failure has no
operation result and does not claim retained facts.
See [bulk lifecycle operations](changes/056-bulk-task-lifecycle.md).

Initial assignment uses the same collector and per-task assignment publication
owner through `task assign CONTRACTS... WORKER_ID --reason TEXT`. One target
retains its original direct key and result; multiple targets derive stable keys
from the batch base and exact Contract ID. Each uses the original empty
predecessor and fresh-assignment requirement. Assignment's deliberate application
errors retain `ValueError` compatibility but have a narrow type for collection;
unclassified failures stop instead of becoming ordinary refusals. Accepted-pending
responses without exact result data also stop subsequent calls. No batch ledger,
effect, claim reset or cross-task transaction is introduced. See
[change 067](changes/067-bulk-initial-task-assignment.md).

Explicit bulk replacement and clear use `task reassign CONTRACTS...` with one explicit
nonempty predecessor mapping per target. One replacement Worker or one clear
action applies to the invocation. The adapter validates all maps before Store,
identity or Redis access, then delegates each exact head to the same assignment
validator and publisher with `require_assigned=True`. Keys bind action, batch
base, exact Contract and predecessor. Every result echoes the requested head
as input context; this echo is neither fact proof nor authority. Exact-data
accepted pending continues through ordinary per-target pre-sync, whereas
accepted pending without result and unknown errors stop collection. Historical
single-target syntax and direct keys remain supported. See
[change 068](changes/068-bulk-task-reassignment.md).

Routine `--current` operation preparation lives in the shared application module
`task_assignment_requests.py`. It reads all selected original heads from one
immutable snapshot and saves canonical input bytes under a digest-named private
file before target publication. `--resume REQUEST_ID` loads the original ordered
targets, heads, mode, Worker, reason and base; it never refreshes heads or accepts
intent overrides. Both routes reuse the explicit batch target executor and signed
assignment publisher. New modes return a batch envelope even for one target;
expert single-target output and keys remain unchanged.

Saved requests bind schema, resolved Store path, Genesis and observed revision.
The reader checks canonical bytes/digest/shape, bounded regular private files and
workspace/domain identity, and re-establishes file/directory durability before
recovery. Saved inputs hold no authorization, progress or acceptance state.
Hash addressing detects changes at the original request ID; it is not a
signature or a defense against coordinated local replacement. Path binding
refuses automatic resume after relocation or into a copy at another path. Exact
committed members replay original facts under current authority; stale unattempted
members refuse through the normal owner. See
[change 069](changes/069-reassignment-request-recovery.md).

Read-only task attempt inspection has one application owner in
`task_diagnostics.py` and a thin CLI adapter. `task runs` reconstructs allocations
and their proven outcomes; `task diagnostics` selects an exact claim's safe causal
facts. The owner fixes a SQLite revision and UTC observation time in each cursor,
validates receipt/head/member and owner-specific transition ancestry, and reports
selected-scope integrity errors regardless of the displayed page. Standalone
authentication audit IDs cannot join a valid attempt. Inspection neither resets
claims nor repairs historical facts. The current immutable task has zero or one
grant; successor work is inspected under its new Contract ID.

Grant provenance accepts the existing signed command's explicit null selector
for domain-wide selection as well as an exact pinned Contract. The request,
receipt, Worker/key and revision order remain bound to the actual grant.
Malformed proof containers and enums return integrity issues before iteration
or membership checks. This is historical inspection, not fresh authentication
or a change to the claim writer. See
[change 060](changes/060-domainwide-claim-diagnostics.md).

Output uses typed identifiers and numeric usage, suppressing arbitrary bodies,
unknown categories and unsafe legacy metadata. The finite safe category list
includes `provider_request_budget_exhausted`; other unrecognized categories
remain suppressed. A noncanonical package display is null while internal proof
compares its original value. Redis synchronization is
required; SQLite remains the history authority. Cursor pagination bounds output,
while the current implementation scans runtime history. These summaries do not
capture Worker I/O. See [change 058](changes/058-task-attempt-diagnostics.md).

Task lineage inspection uses one immutable task-view snapshot to navigate the
selected task's connected component through published containment, supported
system tool-method recovery publications and canonical signed retry
publications, and accepted leaf, expanded and edit-through-handoff successor
publications, plus accepted review-linked task revisions. Both containment
endpoints must match their exact published declarations. Retry edges require
the accepted target Candidate's exact source
receipt parent, a sole declaration effect, both exact publication proofs and
the complete canonical retry successor shape; source declarations may be
compound publications. Receipt, committed-head and member proofs use the
generic indexed history owner. Handoff edges bind the typed link, cancellation
terminal and successor declaration to exact accepted publications for both
materialized endpoints; source publications may be compound. The relation view
does not re-audit historical handoff budget, tree, hold, assignment or allowance
decisions. Recovery edges prove the source Contract's
accepted publication relation, while invocation failure, claim expiration,
admitted output rejection and scheduling facts remain separate source
proofs and diagnostics. Legacy generic recovery retains its existing source-proof limit. Recovery diagnostics do not establish a one-to-one
failure-to-publication mapping. Each member retains its canonical
task status and blockers; unverified evidence cannot expand the component.
Executable method recovery accepts only a canonical system successor whose
direct predecessor has an exact authenticated claim-failure binding and whose
full deterministic intent matches the accepted declaration and context member
on one committed head. Recursive markers repeat that proof at each edge;
context metadata only selects source facts for revalidation. Generic repair
keeps its legacy outcome payload, including for ordinary JSON that happens to
contain reserved recovery-marker names. Claim-proven unavailable outcomes may
show their fixed reason and a reported target in read-only diagnostics; that
observation does not verify the target or create a lineage edge.
Operational method-recovery preparation uses one owned SQLite snapshot for
history, exact source and ancestor binding proofs. It closes that copy before
scheduling and retains only frozen history, without a query tied to the closed
Store. The recovery Candidate is published through the original live runtime
and publisher with current writer checks. A provided snapshot failure is not a
fallback to live reads; trusted Stores without that capability retain their
existing memory path. Generic task-view query identity and revision matching
remain strict. See [change 117](changes/117-snapshot-recovery-source-preparation.md).
Read-only lineage, retry, recovery and attachment readers use one pure
effect-type sequence reader; malformed containers such as mappings cannot
qualify retry, recovery, handoff or revision edges.
Handoff and revision share one pure successor-publication proof while their
feature wrappers retain fixed sole-effect checks and issue types. A revision
edge proves the exact source cancellation, successor declaration and accepted
`task.revised` link; it does not re-audit the historical review decision, report,
allowance, assignment or present-day eligibility. This guards read
interpretation and does not change Candidate commitment.
Redis is required. This view adds no group status, claim authority, recovery
scheduling or output qualification. Unavailable schedule records are only
observations, and containment does not prove a tool-observation-to-continuation
causal edge. Agents may continue through the root's ordinary claim flow or
configured Fleet when supported; explicit `/fail` remains an operator-decided
terminal result without automatic recovery. See [change 098](changes/098-executable-method-recovery.md),
[change 099](changes/099-tool-mcp-failure-convergence.md) and [change 080](changes/080-verified-task-lineage.md),
[change 082](changes/082-retry-publication-lineage.md),
[change 083](changes/083-handoff-publication-lineage.md), and
[change 085](changes/085-task-revision-lineage.md).

Current task context inspection has one application owner in `task_context.py`.
It uses canonical disclosure and shares the extracted method-context reader
with Worker package construction. Default output contains metadata only; exact
Asset selection plus explicit content opt-in returns its disclosed representation.
Redaction remains intact, and nonpromptable or tombstoned bodies are refused.
Legacy selection behavior is preserved, including metadata for such Assets when
selected. The immutable SQLite snapshot and required Redis synchronization do
not write facts or claims. A current preview is neither historical claim-package
evidence nor readiness, authority or qualification. See
[change 062](changes/062-task-context-view.md).

Local Worker operational capture has one application owner in `task_logs.py`.
Outer quick, pool, target and configured/Fleet execution delegates to the
canonical runtime with an invocation-scoped observer; nested Engine execution
masks inherited observers. Built-in LLM retry branches emit typed metadata at
its actual source. There is no root logger or arbitrary stdout interception.

The Store-adjacent spool has exclusive private files, per-claim/workspace caps
and nonblocking quota locks. Files are auxiliary observations, never facts,
Assets, task state or qualification. Write failures produce separate safe run
warnings. Synchronous filesystem operations can add latency; no hard deadline
is claimed. Captures are not overwritten on restart or pruned automatically.

Read/follow reuses the diagnostics exact claim-proof owner on a closed-per-poll
SQLite snapshot and requires Redis. Headers bind domain/task/claim/epoch/package/
Worker; cursors bind physical root, capture and verified prefix. Strict event
schemas omit bodies and arbitrary error text. A hash chain detects accidental
mutation, not authentication. Invocation finish precedes envelope/sign/submit,
and EOF is not task completion. See
[change 063](changes/063-local-worker-operational-logs.md) and
[ADR-044](docs/adr/ADR-044-local-worker-operational-logs.md).

An opt-in local provider profile shares one process-scoped request meter across
its ordinary Planner and Researcher transports. Four typed auxiliary events
separate permits, transport entry, bounded reply observation and finalization;
usage unknowns remain unknown. No event grants task authority, establishes
provider receipt or proves billing. A new process starts a new budget epoch.
The current fixed profile and exact schemas are described in
[change127](changes/127-local-runtime-provider-meter.md).

The explicit direct example projects native provider call/function envelopes
onto required validated fields and discards unused metadata. Literal empty
tool-call content becomes canonical null without changing the original input;
the strict offline envelope and action/authority owners remain unchanged. See
[change128](changes/128-native-tool-response-compatibility.md).

Redis event capture builds a local typed Candidate-head context from its exact
effective record map. All matching heads retain global source order and resolve
through the original exact-member reader. Malformed Candidate IDs keep the
legacy lazy fallback. No context crosses a capture or source refresh; full
prefix proof remains unchanged. See
[change129](changes/129-capture-trace-reference-index.md).

Task dependencies use one signed `task.dependency.link` or
`task.dependency.unlink` effect with explicit `task.dependency.manage` authority.
The plugin projects generic `claim.constraint.placed/released` facts; the
placement ID is the exact link ID. One pure core owner validates schema, open
edges, cycles, exact release pairs and typed prerequisite terminals. Commitment
binds receipt/head/actor/key and rechecks current lifecycle/claims/graph under
the writer lock; SQLite claim acquisition reads that same predicate in its
transaction. Scans and Redis views are advisory. Task projections use their
selected historical fact set; no-edge views retain prior hash behavior.

Placement requires known unequal endpoints and a live, never-claimed target
without satisfied nonempty outputs. Typed prerequisite `complete` satisfies an
open edge; other terminal states, missing or conflicting history remain blockers.
Unlink releases one exact open placement on a live, never-claimed target. Manual
holds allow safe hold→unlink→link→unblock replacement. Exact signed replay precedes
current mutable checks; changed inputs yield a new Candidate and fresh validation.
These facts affect only new claims, never Contract inputs, scope, output
satisfaction or qualification. Canonical output completion remains possible.

Successor publication refuses cancelled targets with unfinished prerequisites
outside the cancelled set, preserving internal old edges as history and all
protected branches. Same-batch authorized completion repairs satisfy prerequisites;
repair-only targets are exempt. Outside downstream targets keep exact cancelled
prerequisite IDs and need explicit replacement; no automatic successor following
or dependency-recursive cancellation occurs. Schema20 refuses normal old-reader
reopen and unsupported pre-20 constraint imports. MemoryStore does not gain an
operational claim API. See [ADR-036](docs/adr/ADR-036-task-prerequisite-claim-constraints.md).

Task assignment publishes one signed `task.assign` effect with
`task.assignment.manage`. Initial set, replace and clear emit the reserved
`claim.assignment.changed` fact. One pure core owner selects the exact head,
including a cleared head, and validates its seven-field payload and causal
provenance. Live targets must have no claim history and no satisfied nonempty
output obligation. Enabled static compatibility permits placement even when
the Worker is busy; claim capacity and current identity remain writer checks.
SQLite enforces the selected Worker inside claim acquisition, including trusted
direct Store calls. Assignment does not grant capabilities or control output
qualification. Views read the selected facts and expose registration/capacity
blockers; Redis schema-v2 events carry typed Worker references without authority.

Handoff and revision inherit assignment atomically through the same owning
effect's cancellation/declaration/link witness. Explicit set/clear signs an
exact source head and Worker override, with additional assignment authority.
Frozen handler admission and core-generated per-effect owners/intents/witnesses
participate in the locked projection identity check. Ordinary children inherit
no assignment. No-history default successors retain existing wire identities.
Exact committed replay verifies original receipt/head/membership and historical
registration parents before skipping mutable eligibility checks. Schema21 gates
normal readers and refuses pre-21 assignment imports. See
[ADR-038](docs/adr/ADR-038-transactional-task-assignments.md).

Active business-task handoff uses a dedicated `task.handoff` plugin effect, also
requiring `contract.cancel` and `contract.publish`. SQLite atomically cancels
the predecessor, releases its claim, reserves bounded remaining allowance and
declares a successor. Late submission under the old claim is fenced. Only
unfinished output slots are declared; completed slot names and exact Asset IDs
remain link metadata. Ordinary tasks retain global same-name business-output
matching; independent-policy tasks require exact Contract qualification.
The successor inherits frozen execution/disclosure scope and acceptance policy.
For ordinary handoff the operator's explicit reason is appended to its description and is Worker
context; retained Asset contents are not automatically added to that context.
An optional successor hold additionally requires `task.block`. Expanded handoff
closes selected live descendants and transfers the original unfinished root
obligation to one ordinary successor, which can plan afresh. It does not clone
the old execution tree. The shared completion predicate preserves whole branches
serving a live independent ancestor outside the cancelled root. Holds on transferred
or completion-repair nodes block with exact IDs; terminal intermediaries and
preserved external branches retain their own history.

The generic allowance owner credits the source and debits each selected live
donor's unused residual through exact committed funding paths. It validates
returns before the ordinary successor reservation and independently under the
SQLite writer lock; replay cannot create more credit. Previously terminal funds
are not recovered. Source balance zero is supported when eligible descendants
hold remaining allowance. Schema19 refuses older readers and migration refuses
preexisting return rows because schema18 has no supported return producer.
Expanded successors freeze current legal source disclosure with v6 input bindings;
child results, observations and authorities are not copied. Historical leaf
handoff identities, payloads and operator key remain unchanged.

For a nonempty output-satisfied descendant lacking a typed terminal, signed handoff
explicitly publishes a canonical `complete` terminal bound to exact output evidence,
fences its claim and extinguishes its own residual. It continues through live
grandchildren. Untyped old failure/cancellation/unreachable traces are visible
history-conflict blockers. Audit and rebuild remain read-only. Source eligibility
still excludes terminal, system/recovery, plugin-controlled or protected-minting
tasks. Cancellation does not stop an OS process or reverse external effects.
Polling late rejection for already non-failed terminal work records unavailable
recovery with `terminal_preserved`, without new recovery tasks or a conflicting
failed trace. Failed-task recovery remains available. The separate precheck to
recovery-publication cancellation race is not yet transactionally qualified.

The request signs a nullable budget. Exact replay returns the first committed
successor and budget before checking the predecessor's later state. For accepted
Candidates handled by a trusted injected registry, commitment reruns the pure
decision with current facts and effective keys inside the writer transaction.
Changed projection identities cause a durable rejection before writes. The
kernel owns this generic check; it does not import feature semantics. Current
and historical views read terminal facts directly, including cancellation
without a terminal trace. See
[ADR-030](docs/adr/ADR-030-transactional-task-handoff.md),
[ADR-034](docs/adr/ADR-034-paired-causal-allowance-returns.md) and
[ADR-035](docs/adr/ADR-035-expanded-root-handoff.md).

Task text editing is an optional strict `edit` object on the same sole handoff
effect, requiring explicit `task.edit` authority. It replaces name/description
exactly, preserving omitted text without a handoff suffix or appended reason.
Titles are nonblank and bodies may be empty; valid UTF-8 text is bounded to
65536 bytes per field, with canonical Unicode NFC no-ops rejected. The request
binds canonical source declaration, exact assignment head and complete sorted
source hold set. The edit inventory allows source holds only, atomically copying
up to 64 reasons and placements to the successor; old placements remain unchanged
and descendant holds still block. Copied placements additionally require
`task.block` and bind the receipt, old placement and successor declaration.
The shared transfer link carries declaration references, supplied field names
and the exact old/new map; full text stays in Contract declarations. Assignment
inherits and priority does not. Fresh writer revalidation rejects changed
snapshots. Exact replay verifies signed receipt/full-head membership and copied
provenance before mutable source checks, retaining current-key authorization.
The application returns the durable successor and exact replay snapshot and
reports accepted Redis synchronization pending with retained facts/result.
There is no edit-specific kernel state, schema or scheduler. See
[ADR-043](docs/adr/ADR-043-text-edit-through-shared-handoff.md).

Injected `EffectHandler` descriptors separately declare complete direct fact
types and optional exclusive reserved types. All in-tree operational registries
use strict declarations. The old assignment metadata is unchanged; external
typed and tuple handlers with `direct_output_fact_types=None` retain trusted
compatibility. Frozen mappings reject duplicate exclusive owners, and each
projector record is checked before generic co-effects are added. Projectors
must return a tuple of canonical RuntimeRecords; invalid containers,
members or identities cause observable rejection. Sole-effect
exact full-head replay verifies the authenticated receipt, committed head,
ordered members and record equality; only generic allowance grant/reservation
and asset materialization may fall outside the strict set. Reserved ownership
still applies to every member. Multi-effect replay has no full-head exception.
The existing owner fingerprint and writer re-projection carry this decision,
without a Store ledger or schema change. See
[ADR-040](docs/adr/ADR-040-injected-direct-fact-ownership.md).

Independent task review is plugin-owned. A sole-effect signed request creates a
bounded ordinary reviewer child and an exact target/policy link. Only original,
promptable target/rubric/evidence Asset IDs enter its context; name rebinding is
not used. The child has no tools, delegation or acceptance policy and runs through
ordinary Worker claims. `run --worker-id` selects a durable execution identity
without attestation authority; configured fleets retain their configured IDs.

A sole-effect decision checks exact committed report ancestry and a report
producer distinct from the target producer, then invokes canonical attestation
with the original frozen policy evidence. Its actor also needs the policy's
verifier capabilities and must differ from the target producer. The new report
is linked separately rather than added to frozen evidence. Accept can qualify
and complete the source; changes records rejected attestation, without publishing
revision work. Exact replay restores committed projection before mutable state
or duplicate-verifier checks; the writer snapshot fence arbitrates decisions.

Attestation, task review, and submitted-output views require the selected Asset
to be an exact member of its producer Contract's declared output and of the
producer Candidate's committed head, with a matching `claim.submitted` fact and
claim grant. A graph Asset's signed source URI alone is not output provenance.
The proof keeps the graph assertion signer separate from the outer Worker who
publishes the accepted Candidate.
This changes no Candidate schema, Store schema, or existing materialization and
does not rewrite historical facts. Deployments must stop old Worker and
committer processes before relying on the new qualification boundary; an
already-open old process is not fenced by a reader floor. The ordinary
single-task, single-round claim and submission flow is unchanged.

The current review slice accepts live independent-policy business sources, not
retrospective gates for ordinary work. Default decision authority is fixed to
`verify.human`; custom keys must already be authorized. Report-task completion
keeps ordinary global same-name matching, so it is not report lineage or source
qualification proof. An exact foreign report is rejected independently, even if
it completed the reviewer task. Source allowance may be insufficient for later
revision, and distinct actors do not prove distinct people. See
[ADR-031](docs/adr/ADR-031-policy-bound-task-review.md).
A separate sole-effect `task.revise` plugin Candidate references a committed
changes decision and its canonical rejected attestation. Its shared pure reference
validator serves application preflight and projection, checking exact request,
policy, target, report, causal provenance and distinct report producer. Cancellation,
claim fencing, bounded source allowance reservation, successor declaration and
`task.revised` link commit atomically. All still-unqualified output slots transfer;
qualified slots and exact IDs remain history metadata. The source stays cancelled,
and its rejected output cannot qualify the successor.

The child retains original execution and acceptance scope and input roles. Exact
input bindings and frozen legacy label references preserve the original disclosure;
only the old exact target/report are added as context, never as new policy evidence.
Canonical disclosure IDs and views must match. The reason becomes explicit Worker
context. Null budget resolves once and exact replay restores its original successor
before later state checks. Positive allowance, live independent business source,
no holds and no unfinished descendants are required. Expanded-tree transfer remains
outside this path. Continue the returned ID through ordinary `run --task`; publishing
revision does not invoke a provider. See
[ADR-033](docs/adr/ADR-033-review-linked-task-revisions.md).

Authentication lost through concurrent key revocation is recorded without
reusing an invalid receipt. Handoff and revision event metadata reference both predecessor
and successor; operator text is not parsed for task references.

The CLI provides domain initialization, task and Asset publication, Worker
execution, trace, audit, replay, recovery, and status views. The optional
FastAPI adapter creates one Store connection per request and closes it
deterministically.

`aig asset show NAME` retains its existing name-based selection. Alternatively,
`aig asset show --id ASSET_ID` selects the exact stored Asset through the same
query projection, without name fallback. Both are local operator inspection;
they do not filter task disclosure or establish output qualification. Inspection
redacts bodies whose `disclosure_view` is not `original`, and reports that view;
`promptable=false` alone does not hide an original body from operator inspection.
The HTTP Asset response uses the same projection for reads and publication
responses. Slicing retains the source Asset's `disclosure_view` and
`promptable` classification, so derived bodies cannot silently become original
or Worker-disclosable. Use `task context` for disclosed task inputs and
`task result` for qualified output export. See
[change 070](changes/070-exact-asset-inspection.md) and
[change 075](changes/075-asset-read-redaction.md).

Quick-goal `aig run GOAL` assigns a unique declared output name to each
invocation. It uses the goal and only the UTF-8 files explicitly supplied with
`--input NAME=PATH` (each at most 1 MiB); it does not scan the workspace, fetch
network sources, or load operator tools. Successful exportable text is saved as
Markdown to `--output PATH`, defaulting to `.aig/outputs/<session>.md`; existing
destinations are not overwritten. Each quick run claims only its root Contract
or current descendants. Ordinary task outputs still use domain-wide same-name
satisfaction, so explicitly shared Assets remain reusable. Runtime maintenance
and expired-claim processing remain domain-wide; quick-run claim selection does
not isolate every Store side effect. A failed root remains terminal; its
recovery successor is a separate task and must be run explicitly with
`aig run --task <contract_id>`. For `aig run GOAL --json`, use the returned
Contract's `outputs` and session `produced_asset_ids`, rather than assuming a
fixed output name.

`aig run --task CONTRACT_ID` checks an already terminal target before executing
Worker work and claims only that Contract and its immutable descendants.
`aig run --once` still claims one task from the domain-wide local pool. Both
commands retain domain-wide runtime maintenance. Target-run output preserves
the projected status and blockers and adds a stop reason and operator next
step; a root recovery successor remains an explicit separate run. Timeout and
poll interval inputs must be finite positive numbers. `--max-output-tokens`,
`--thinking-mode`, and optional `--response-format json_object` configure the
LLM adapter. JSON wire mode asks the provider for a complete JSON response and
then compiles only the canonical action wrapper; strict parsing still rejects
malformed or unknown fields. It cannot be combined with `--tool-registry`.
These controls affect provider execution only; they do not change output
authority or independently validate meaning. `--json` remains a CLI output
selection and is unrelated to provider wire mode.

Configured `aig run --config PATH` prepares explicit input files, Skill Assets,
one root Contract and existing Fleet profiles outside the kernel. Shared
application services own signed local publication and Fleet assembly; the old
`fleet run` command delegates to those services. `aig -C DIRECTORY` selects the
workspace. Resource paths are preset-relative while the Fleet database remains
workspace-relative. Invalid resources and Contracts fail before runtime
provisioning. Setup and execution are separate signed transactions.

For new configured roots, each declared input role binds the exact Asset ID
returned by that run's resource publication through the existing v6 input guard.
Other sensitive-policy fields and unused bindings remain intact; conflicting
explicit active bindings refuse root publication. Historical same-name inputs
cannot join the frozen selection. Resume never regenerates this map. Roots with
no inputs and no sensitive policy retain their existing identity. See
[change 109](changes/109-configured-input-pins.md).

`--resume CONTRACT_ID` (or existing `--task`) selects committed work. Configured
resume reads only Worker profiles (directly or through a preset Fleet reference)
and the existing Contract; it never reloads
setup, rebinds labels or resets claims. New preset starts reject existing roots
and output slots present at the startup check; this is an application guard,
not a transactional namespace reservation. Exact acceptance evidence can be bound from
explicit input names to accepted Asset IDs. These are application conventions,
not new authority or namespace isolation rules. See
[configured runs](docs/reference/configured-runs.md) and ADR-026.

An operator may explicitly load a local `ToolRegistry` factory for `aig run`.
The adapter publishes its public descriptors through Candidate commitment,
passes only Contract-scoped definitions to the provider, and hosts tool calls
on a separately registered capability-routed ToolWorker. Registry code is
trusted local code and is never loaded by default.

Fleet also accepts operator-configured `kind = "local"` Workers from installed
`module:factory` references. Loading is explicit, outside method packages, and
uses the existing WorkerHost and Candidate path. Registration metadata comes
from the fleet profile. The business claim validators consume disclosed Assets,
produce structural assessments or terminal gate failures, and never assert
semantic correctness. A local factory is trusted installed code and an explicit
operator authority; it is not downloaded or selected by a task or model. See
ADR-024.

The guarded claim-review adapter composes five ordinary stages: a deterministic
draft assessor, one model-backed revision generation, a deterministic structural
gate, one model-backed semantic review, and a dedicated acceptor. The acceptor
attests only after checking exact input and handoff Asset IDs and their
committed producers. The adapter proves literal evidence bindings and quote
membership; it does not prove semantic truth. One revision generation is the
workflow bound, while normal runtime retries remain protocol attempts rather
than a promise of one HTTP request forever.

Fleet TOML parsing is strict: worker kinds, list fields, capacities, timeout
values, and retry counts are validated before launch. Timeouts must be finite
and positive and retries must be non-negative integers. The default network LLM
adapter reads at most 4 MiB plus one byte to classify an oversized response;
custom decoded `Transport` mappings are trusted adapter inputs and do not pass
through that wire-byte bound.

`ToolSpec` is the local executable tool contract. It binds a deterministic
input/output JSON-schema subset, version, and maximum UTF-8 result size. The
registry validates inputs before invoking a handler and validates JSON results
and byte limits before a successful observation is returned. The ToolWorker
also checks that the signed descriptor matches the registered executable
contract, failing closed on descriptor drift. Matching examines only disclosed
descriptors and requires one distinct matching Asset ID; ordering cannot select
an old version, and multiple matching IDs fail observably. The CLI can publish
version, output schema and byte limit, and inspect an exact committed descriptor
ID; ambiguous names require that exact selector. See
[change 106](changes/106-complete-tool-descriptor-cli.md) and
[change 108](changes/108-disclosed-tool-descriptor-selection.md).
Non-object tool arguments are
rejected before execution. Schema constants and enums distinguish JSON booleans
from numbers, and explicit null schema keywords are invalid except `const`.
An adapter may explicitly require a success envelope field through its output
schema. A schema-valid observation does not universally classify business truth.
In strict provider JSON-action mode, the adapter recursively thaws validated
non-exec payloads before canonical serialization. Nested tool arguments,
parallel calls and attestation outputs retain their JSON values; duplicate keys,
invalid constants, unsupported actions and malformed wire remain rejected.
Plugin semantics and authority still run at the ordinary Candidate boundary.
See [change 115](changes/115-thaw-nested-json-action-payloads.md).
Each observation Candidate
includes structured execution metadata for tool version, duration, result
bytes, error type, and retryability.

The read-only `task status` and `task audit --json` commands use a consistent,
read-only SQLite snapshot. A terminal `task wait` result is checked against
its snapshot; a timed-out wait explicitly reports that terminal integrity was
not checked. The local integrity result validates RuntimeRecord content
addresses and compares materialized runtime views with a strict rebuild on an
independent in-memory snapshot. A failed check leaves projected business
status visible, marks `ok` false, and returns a nonzero CLI status. Findings
include categories and IDs, not record payloads.

Task audit supplements Contract-scoped trace output with
`related_runtime_traces`: commitment-sentinel trace records are included only
when their candidate and committed-record links prove a reference to the
requested Contract. Entries report trace and RuntimeRecord IDs, event type,
revision, and record timestamp; an unproven sentinel is not attributed.

These checks establish consistency only among the available records and local
materialized views. They do not prove runtime-log completeness, prevent a
coordinated database rewrite, or verify actor signatures against trusted
Genesis and authorized keys. Those guarantees require an external trusted
checkpoint and signature verification. The productivity projection walks the
immutable Contract lineage and durable RuntimeRecords/trace; it reports tool
calls, continuations, recoveries, rejections, terminal statuses, and known
usage metadata. It remains an operator view, not a scheduler, budget owner, or
alternate commitment path.

The runtime-only AI4S acceptance path can therefore use the public Fleet and
WorkerHost path for staged `/plan` compilation, a tool observation,
continuation, independent `/attest`, root qualification, and SQLite reopen.
This proves the local runtime loop, not an end-to-end production integration.
MCP production transport, process-level timeout/cancellation/isolation,
exactly-once external side effects, and cross-machine Store/Worker discovery
remain outside the implemented boundary.

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
Read-only projections depend on narrow Asset, Contract, RuntimeRecord, and trace
capabilities rather than the complete commitment Store. SQLite row decoding is
isolated in a pure materialization module; the SQLiteStore continues to own its
single connection and every authoritative transaction.

An optional external trace sink is a post-commit export adapter. It is never
consulted during Candidate reduction or SQLite commitment, and export failure
cannot roll back or redefine already committed trace facts.

## Release diagnostics

`aigineering.diagnostics` is an application adapter. It opens a source SQLite
file read-only, retains a consistent backup, and rebuilds a second private copy.
It compares semantic digests and immutable-record fingerprints and retains
mismatch/error evidence without modifying the source. Manifest output excludes
raw rows and exception text. Verification requires the current schema and never
silently migrates historical evidence. See ADR-021 and
[`release evidence`](docs/reference/release-evidence.md).

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

The v0.5.13.dev0 development tree extends the local reference runtime. It does
not provide:

- a cross-machine distributed Store;
- a consensus implementation;
- a public-network security profile;
- an external security audit;
- a guarantee that authorized model content is true;
- a generic workflow engine.

The release claim is bounded by the tests, conformance vectors, artifact checks,
and real-LLM scenarios recorded in `reports/`.

Planning compile stages retain the original parent's full acceptance policy.
Independent qualification and ordinary output completion share unfinished-child
cancellation, including the intermediate compile task, within commitment.
Cancellation traverses all descendant levels through output-satisfied or already
terminal intermediate Contracts, preserving their existing outcomes. A live
branch serving an outer independent policy remains protected until that
obligation closes. Newly cancelled descendants release claims atomically and
reject late submissions. This corrects deeper work remaining claimable after
parent completion; historical already-completed trees require separately
authorized repair and are not rewritten by rebuilding views.
See [ADR-025](docs/adr/ADR-025-guarded-review-closure.md) and
[guarded review evidence](reports/065-guarded-claim-review-2026-09-13.md).
