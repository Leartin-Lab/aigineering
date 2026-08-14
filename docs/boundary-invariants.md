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

Undeclared names — even if parseable — are rejected with `authority_rejection`.
The contract's `outputs` list is the exclusive allow-list. No name-mutation,
no fallback, no silent acceptance.

## 3. Reserved namespace is runtime-only

Reserved prefixes (`_sys_`, `_tool_obs_`, `_memory_`, etc.) cannot be minted by
an LLM or `/exec` call. Names starting with any reserved prefix are rejected
with `protected_name_rejection` regardless of whether they appear in the
contract's declared outputs.

## 4. Projection is pure

Candidate reduction and typed-effect projection are pure decision functions.
They never mutate the Store or commit side effects. Commitment is a separate,
explicit boundary downstream.

## 5. All rejection paths are traced

Every rejected Candidate — whether from parsing, authority, containment, or a
deterministic commitment conflict — produces a durable rejection fact and
trace with a human-readable reason. Fragment-level projection rejections remain
attached to the same audit trail. No rejection is silent.

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
commands. Across a transport boundary, claim and renewal prove possession of
the enabled Worker's registered key; a self-reported `worker_id` is not
identity. SQLite atomically records the authenticated command with the claim or
renewal and rejects replay of a committed command. Submission validates the
claim, worker identity, fencing epoch, lease, and package binding before
projection. A failed invocation or expired lease becomes a durable terminal
fact and schedules a new recovery Contract.

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
