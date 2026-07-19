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

The `project_candidate()` function is a pure decision function. It never mutates
the store, never commits side-effects. The `ProjectionResult` dataclass is
frozen. Commit is a separate, explicit step downstream.

## 5. All rejection paths are traced

Every rejected candidate — whether from parse error, duplicate conflict,
authority rejection, or protected-name collision — is recorded with a
`RejectionCategory` and a human-readable `reject_reason` in the
`ProjectionResult.rejected_candidates` list. No rejection is silent.

## 6. Delegated behavior is ordinary task publication

Planning and replanning enter through Store-free staged plugins and one
claim-bound signed Candidate containing ordinary Contract declarations. Draft,
dependency analysis, and compile are independently claimable tasks. The source
attempt closes as `expanded`; the root remains unsatisfied until descendant
facts satisfy its outputs. Retry, fail, and tool still use a bounded
compatibility action while their plugins are cut over. No production scheduler
depends on Engine-owned waiting/suspended/resume state.

## 7. Worker pull submission is claim-bound

Operational worker execution uses a `WorkerPackage` and actor-signed Candidate
commands. Across a transport boundary, claim and renewal prove possession of
the enabled Worker's registered key; a self-reported `worker_id` is not
identity. SQLite atomically records the authenticated command with the claim or
renewal and rejects replay of a committed command. Submission validates the
claim, worker identity, fencing epoch, lease, and package binding before
projection. A failed invocation or expired lease becomes a durable terminal
fact and schedules a new recovery Contract.

Hosted `/exec` signs ordinary Asset effects; staged plan/replan signs ordinary
Contract effects. The same SQLite transaction rechecks the binding, commits
facts, and records an immutable attempt outcome (`output_asserted`, `expanded`,
or `failed`).

## 8. Commit is transactional on the SQLite path

For SQLite-backed worker submission, Candidate, projection, accepted assets,
trace events, idempotency, terminal facts, and claim transition commit in one
database transaction. A mid-commit failure rolls the whole submission back.
Derived completion markers and their audit traces likewise commit as one
transaction consequence; a terminal fact and terminal trace cannot disagree.

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
facts; terminal projection commits in the same transaction.

## 12. Recursive work cannot mint causal allowance

Root declarations materialize an immutable allowance grant. Every newly
published child Contract atomically reserves allowance from its causal parent
and receives only that reserved amount. Pure projection rejects oversized
batches; SQLite rechecks the balance in the commit transaction to arbitrate
concurrent publishers. Terminal Contracts extinguish their remaining allowance,
and exact Candidate replay cannot reserve it twice. Allowance is task lineage
authority, never a Worker account or mutable process counter.
