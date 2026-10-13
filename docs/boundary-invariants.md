# Boundary Invariants

This document defines the non-negotiable boundary invariants of the Aigineering agent
runtime. Every invariant is enforced by the commitment boundary — the
candidate-to-fact gate gated by projection and authority — and backed by regression
tests.

---

## 1. Worker output is always a candidate

No worker output enters shared runtime state without passing through both
**projection** (parse + dedup) and **authority** (declared-output + reserved-name
checks). The boundary enforces: raw output → candidate → (projection + authority) →
accepted assets.

Hosted `/exec` output is encoded as one atomic signed
content/definition/assertion graph batch. Only an accepted assertion projects
the compatibility Asset consumed by task completion. The projected Asset ID
binds assertion provenance; reusable content identity does not collapse
different publishers or definitions.

## 2. Only declared outputs become facts

For claim-bound Worker output, undeclared names — even if parseable — are
rejected with `authority_rejection`. The claimed Contract's `outputs` list is
the exclusive allow-list; a Worker cannot widen it. A valid non-claim signed
Candidate, such as an operator publication, is authorized by its current
actor's signed capabilities and is not subject to a Worker's output allow-list.
No name-mutation, fallback, or silent acceptance is permitted.

## 3. Reserved namespace is runtime-only

Reserved names (`_sys_`, `_tool_obs_`, `_memory_`, etc.) do not grant authority
through their spelling or through declaration alone. Publishing one requires
the current signed Candidate actor's `asset.publish.protected` capability, or
a claim-bound Candidate whose already-authorized parent Contract lists that
exact name in `minting_authority`. Without either authority, reserved names are
rejected by the protected-name authority check, including when declared as an output.
This applies to graph definitions and graph assertions that materialize Assets,
as well as the existing `asset.propose` path.

## 4. Projection is pure

Candidate reduction and typed-effect projection are pure decision functions.
They never mutate the Store or commit side effects. Commitment is a separate,
explicit boundary downstream.

## 5. All rejection paths are traced

Every rejected Candidate — whether from parsing, authority, containment, or a
deterministic commitment conflict — produces a durable rejection fact and
trace with a human-readable reason. Fragment-level projection rejections remain
attached to the same audit trail. No rejection is silent.

Authentication failure admits no receipt. Its claimed actor, Candidate ID and
claim binding are diagnostic metadata, never authority for claim submission,
attempt closure, task terminal, allowance or recovery. Persist the audit even
when its claimed ID matches a previously committed Candidate. Authenticated
output rejection keeps the ordinary receipt-backed failure path. Historical
incorrect facts remain immutable and cannot be repaired by read-only rebuild.
See [authentication rejection isolation](../changes/057-authentication-rejection-isolation.md).

## 6. Delegated behavior is ordinary task publication

Planning and replanning enter through Store-free staged plugins and one
claim-bound signed Candidate containing ordinary Contract declarations. Draft,
dependency analysis, and compile are independently claimable tasks. The source
attempt closes as `expanded`; the root remains unsatisfied until descendant
facts satisfy its outputs. Retry, fail, and tool use ordinary contained Contract
declarations in WorkerHost; only the raw external envelope surface retains a
bounded compatibility action. No production scheduler depends on Engine-owned
waiting/suspended/resume state.

Draft and dependency tasks reserve one allowance unit each; compile receives the
remaining lineage grant. Its Worker-local plugin converts one temporary
blueprint directly to claim-bound child declarations. The hosted path does not
commit a `_plan_result_` Asset or wait for a completion callback.

## 7. Worker pull submission is claim-bound

Operational worker execution uses a `WorkerPackage` and actor-signed Candidate
commands. Operational local and HTTP claim and renewal carry the original signed
Candidate. Inside the writer transaction, SQLite checks its exact payload,
signature, current effective key, capability and enabled Worker registration;
a self-reported `worker_id` is not identity. The canonical receipt and request
commit atomically with the claim or renewal. Signed grants reference their
request; signed renewals reference both the original grant and renewal request.
Committed commands are single use; a scan finding no work commits nothing and
may be retried. Trusted recordless Store calls are internal compatibility seams.
See [ADR-037](adr/ADR-037-transactional-worker-authentication.md).
Local hosts additionally retain their resolved registration version from setup
through claim selection and the writer fence. A narrow automatic profile
promotion uses a signed plugin/register pair and rechecks exact head, effective
identity and no active claim inside commitment. Ordinary explicit registration
and external signed command shapes remain unchanged. Profile is descriptive
metadata; stop older local Worker processes before promotion. See
[ADR-039](adr/ADR-039-local-worker-profile-promotion.md).
Submission validates the
claim, worker identity, fencing epoch, lease, and package binding before
projection. A failed invocation or expired lease has a durable outcome. Recovery
publication records whether new work was committed or recovery was unavailable;
an attempted publication is not scheduled recovery.

A terminal fact closes any unrelated active claim for the same Contract in the
same transaction. The release is itself immutable and rebuildable. A stale
claim-bound Candidate cannot publish after cancellation or completion, while
exact replay of an already committed Candidate remains idempotent.

Hosted `/exec` signs ordinary Asset effects; staged plan/replan signs ordinary
Contract effects. The same SQLite transaction rechecks the binding, commits
facts, and records an immutable attempt outcome (`output_asserted`, `expanded`,
or `failed`).

A claim-bound child cannot self-grant protected publication by filling
`minting_authority`. Protected child outputs/templates must be an exact subset
of authority already present on the claimed parent. Routing capabilities, tool
scope, Worker pools, disclosed inputs, labels, and causal allowance are also
contained.

## 8. Commit is transactional on the SQLite path

For SQLite-backed worker submission, Candidate, projection, accepted assets,
trace events, idempotency, terminal facts, and claim transition commit in one
database transaction. A mid-commit failure rolls the whole submission back.
Derived completion markers and their audit traces likewise commit as one
transaction consequence; a terminal fact and terminal trace cannot disagree.
Terminal-driven claim release and its immutable release fact are part of that
same transaction.

## 9. Runtime progress is reconstructable

Contract, Asset, Trace, claim, worker-registration, idempotency, and replacement
materializations are projections of immutable runtime records. Deleting and
rebuilding those views must preserve the semantic digest. Process snapshots and
runtime heartbeat rows are not execution authority.

Registration actor/key binding is part of the verified semantics. Display
timestamps alone are not identity or lifecycle authority.

## 10. No unfinished work ends silently

No enabled work is not equivalent to success. Missing inputs, capability gaps,
budget exhaustion, provider failure, malformed provider output, expired claims,
and terminal conflicts are explicit blockers or failure facts. CLI/API outcomes
must remain non-success until declared outputs are satisfied.
Replay code must not skip a durable failure/rejection fact when its required
causal Contract or raw Candidate evidence is missing. That condition is an
explicit consistency error: silently continuing would leave the same work
permanently unprocessed on every restart.

## 11. Assertion and independent acceptance are distinct

An output Asset is an authorized assertion, not automatic semantic truth. A
Contract whose identity binds `acceptance_policy.mode=independent` remains
unsatisfied until a different actor with every required verifier capability
publishes an `asset.attest` Candidate for that Contract, declared output slot,
and exact Asset ID. Producer self-attestation, wrong-slot Assets, non-task
Assets, missing evidence, and capability gaps fail closed. Accepted
attestation creates reconstructable `asset.attested` and `output.qualified`
facts; terminal projection commits in the same transaction. The exact Asset
may come from the Contract or an immutable descendant, but never from an
unrelated same-name producer.

When the immutable policy declares deterministic `output_shapes`, producer
submission and independent qualification both validate the exact JSON shape.
Planning, recovery, retry, and continuation retain the applicable shape. A
verifier attestation cannot turn mechanically malformed content into a fact.

## 12. Recursive work cannot mint causal allowance

Root declarations materialize an immutable allowance grant. Every newly
published child Contract atomically reserves allowance from its causal parent
and receives only that reserved amount. Pure projection rejects oversized
batches; SQLite rechecks the balance in the commit transaction to arbitrate
concurrent publishers. Terminal Contracts extinguish their remaining allowance,
and exact Candidate replay cannot reserve it twice. Allowance is task lineage
authority, never a Worker account or mutable process counter.

## 13. A signed derivation claim is not derivation proof

Policies that accept a replacement claim type require a valid incoming claim
for the exact disclosed Asset. Exact slices bind source, replacement, lineage,
range, and derivation version, and verification recomputes content from the
committed source. Invalid ranges, UTF-8 byte splits, mismatched content, or an
unsupported derivation version fail closed. Semantic similarity cannot satisfy
an exact derivation policy.

## Acceptance through intermediate plans

A planning compile Contract retains its parent's complete acceptance policy;
unqualified business output cannot complete that intermediate obligation and
cancel its prerequisite verification work. Independent root completion uses the
same unfinished-child cancellation rules as ordinary output completion, in the
same commitment transaction.

Completion traverses all descendant levels, including already-terminal or
output-satisfied intermediate Contracts, without changing their existing terminal
facts. A live branch explicitly serving an outer independent acceptance policy
remains protected until that obligation closes. Every newly cancelled descendant
releases its active claim in the same transaction; late submission remains fenced.
This applies to new completion transactions and does not migrate historical
trees whose terminal facts were already committed.

## 14. Claim holds are transactional facts

An open signed claim hold prevents claim acquisition in the same SQLite
transaction that would grant the claim. Placement rechecks terminal and claim
history inside the commitment transaction; a precomputed task view is not the
arbiter. Release references exactly one open same-Contract placement, and
competing releases cannot both commit. Hold provenance binds its authenticated
Candidate receipt and committed envelope. Exact replay retains the original
fact; terminal outcomes and historical claims are never reset by hold operations.

## 15. Injected effect decisions are rechecked under the writer lock

Accepted Candidates handled by a trusted injected effect registry rerun the same
pure decision against current facts and effective actor keys inside the SQLite
writer transaction. The admitted and fresh immutable projection identities must
match before any batch writes. A changed or rejected decision produces observable
durable rejection; it cannot silently substitute a newly projected batch.
The registry remains frozen, cannot override built-ins, and does not introduce
feature imports into the kernel. Sequential MemoryStore validation is not evidence
of production concurrency arbitration.

Strict injected handlers declare their complete direct fact types separately
from assignment metadata. Exclusive reserved types have one owner in the frozen
map and cannot be emitted by another effect, including a legacy handler. An
invalid projector return container, member or canonical record identity causes
durable rejection before any member fields are interpreted. An
exact sole-effect full-head replay may retain only generic allowance grant,
reservation and asset materialization outside its strict set; every returned
member still passes reserved ownership checks. Original receipt, head, ordered
membership and record equality are required, never a projector-supplied flag.
Compatibility-mode external handlers remain trusted host configuration. See
[ADR-040](adr/ADR-040-injected-direct-fact-ownership.md).

## 16. Review delegates exact qualification to the existing acceptance owner

A review request freezes exact target, rubric and evidence Asset references and
reserves bounded causal allowance for an ordinary report task. Report completion
does not qualify source output. A decision independently checks exact report
provenance and producer separation, then invokes the canonical attestation
projector under the original policy. Newly generated reports cannot widen frozen
evidence. Human verdicts are authorized assertions, not automatic interpretation
of reviewer prose or proof of universal truth. Review cannot rewrite terminals,
reset claims or provision arbitrary verifier authority.

## 17. Exact input binding preserves role-bound policy

A mapped input role reads only its signed exact Asset IDs, without name fallback.
Missing, tombstoned and wrong-role active references fail closed. Original trust,
signer, derivation and redaction checks still apply to selected inputs. Empty maps
or unused bindings grant neither new input roles nor capabilities. Guard-bearing
Contracts require task:v6; older identity domains cannot carry the guard. This
reader gate began with schema18; the current floor is schema21. It is a normal reopen
compatibility gate, not protection for old connections
that were already open.

Revision preserves every unqualified source output obligation, bounded parent
allowance and original policy. The old exact target/report are context only and
cannot widen frozen acceptance evidence. Source cancellation and old-claim fencing
commit with successor publication; new qualification cannot rewrite that terminal.

## 18. Return allowance conservatively through original funding facts

An authorized return credits its recipient and debits its cancelled live donor
once. Exact committed grant/reservation paths, Candidate receipts/heads and donor
cancellation prove the transfer. Validate credits before child reservation and
again under the writer lock; exact replay cannot become fresh credit. Previously
terminal funds cannot return. A terminal intermediary is not revived when a live
grandchild returns directly to its funding ancestor. Feature authorization stays
in the plugin; the kernel owns generic conservation. Schema19 rejects older readers
and refuses migration of preexisting return rows from versions with no producer.
Raw Store append cannot publish balance-changing allowance facts. New ingress
grants/reservations and historical upgrade inputs must match canonical declared
budgets, funding pairs and committed declaration provenance. Store implementation
code remains trusted; local interpreter access and coordinated database rewrites
are not authenticated by these structural checks.

Expanded handoff retains original root scope and freezes legal input selection.
Selected old descendants receive canonical terminals and claim fencing atomically
with one ordinary successor. External independent-verifier branches remain intact.
Historical satisfied descendants without a terminal receive explicit signed
completion repair bound to exact evidence, never cancellation or refund. Untyped
adverse terminal traces block rather than being silently rewritten. Read-only
audit/rebuild cannot publish that repair.


## 19. Prerequisite edges restrict claims without rewriting obligations

Signed placement/release facts have one core graph/status interpreter. The
SQLite writer validates current endpoints, exact open releases, cycles, claim
history and Candidate provenance before publication, and unresolved exact
prerequisites prevent new claims inside their own writer transaction. Only a
canonical typed successful `complete` terminal satisfies the fixed predicate.
Historical views use their selected fact set. Redis never decides the gate.

Edges do not bind input Assets, widen authority or veto canonical output
completion. Failed/cancelled prerequisites retain their original IDs until
explicit authorized replacement, safely fenced with a manual hold. Handoff and
revision cannot discard outgoing unfinished external prerequisites from targets
being cancelled; internal edges remain old history, repair-only completion is
exempt, and downstream tasks never silently follow successors. Schema20 prevents
normal reopen by readers unaware of these restrictions, with already-open old
connections outside the guarantee.

## 20. Assignment restricts claims without transferring an attempt

One reserved assignment fact and one pure owner determine the exact current
head, including clear. Signed publication and SQLite claim acquisition recheck
that restriction under their writer transactions. All claim history forbids
in-place changes; current Worker identity and capacity still gate claims.
Assignment grants neither capability nor output qualification. Ordinary children
inherit no restriction. Same-obligation successors inherit it atomically or
carry an additional authorized exact-head override, bound to cancellation,
declaration and link by the same-effect structural witness. Exact replay proves
original committed provenance before skipping mutable eligibility checks.
Views and Redis Worker references cannot arbitrate this boundary.

## 21. Advisory priority cannot grant claim eligibility

The application ordering service may reorder only its supplied candidate IDs.
Runtime validates a complete permutation and retains the original Contract
objects. Explicit pins bypass advisory sorting; scoped enumerations preserve
their original scope. Signed priority history and current-head edits remain
plugin-owned, with exclusive fact ownership and writer re-projection. Neither
the hint nor its view can reset a claim or bypass routing, assignment, holds,
dependencies, capability, capacity, allowance or terminal checks. Redis is not
a ranking authority. No global order, fairness or successor inheritance follows
from the hint. See [ADR-041](adr/ADR-041-advisory-task-priority.md).

## 22. Manual recovery cannot grant readiness

Promotion signs the exact complete observed hold set and atomically releases it
under current-key and writer-snapshot checks. New or competing hold transitions
cannot silently alter that signed set. Generic hold batches remain bounded,
homogeneous and confined to one Contract with distinct transitions. Promotion
audit is not task status, claim reset, output acceptance or completion. All other
eligibility owners remain authoritative. Exact original replay restores only its
original releases, preserving later holds. Multi-task invocation is per-task
atomic rather than globally atomic. See
[ADR-042](adr/ADR-042-exact-hold-set-promotion.md).

## 23. Instruction editing cannot mutate a claimed scope

Editing uses the shared signed handoff owner to cancel and fence the predecessor
and declare one bounded successor atomically. It requires explicit `task.edit`
authority and preserves existing execution and acceptance boundaries. Exact
source declaration, assignment head and complete hold set bind signed intent;
writer revalidation rejects snapshot changes. Source holds are copied with exact
reasons and causal provenance in the same transaction, without releasing old
placements or exposing an unheld successor. Descendant holds retain their fence.
Exact replay verifies original committed provenance under current authorization
before mutable checks. Old outputs and transferred audit references cannot
qualify new work or widen disclosure. History/rebuild cannot mutate scope or
publish completion. See [ADR-043](adr/ADR-043-text-edit-through-shared-handoff.md).

## 24. File associations preserve task authority

A task attachment relation is an attributed signed plugin fact with exact task
declaration and original publication witnesses. Current key/capability and pure
projection are rechecked through the existing commitment writer. Historical
receipt/head/member inspection does not reconstruct unavailable original
Candidate signing payloads. The relation cannot mutate inputs, labels, Worker
context, tools, allowance, claims, task lifecycle or output qualification.

Full selected relation history is checked before pagination/export. Read-only
Store snapshots preserve source facts and projections. Copying bytes requires
current original availability and equality to the exact pinned committed Asset,
then the common artifact byte validator. Exclusive private destination creation
never overwrites another file; a partial write remains failure. Relay outages
retain accepted relation IDs and exact replay recovers synchronization. See
[ADR-045](adr/ADR-045-signed-task-attachment-links.md).
