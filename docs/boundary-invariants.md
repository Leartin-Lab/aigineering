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

## 6. Methods are explicit subtasks

Planning, replanning, retry, and tool execution enter the system as method
contracts. Method results and follow-up work are Assets and new Contracts; no
production scheduler depends on an Engine-owned waiting/suspended state.
Method handlers operate through `MethodRuntime`, not Engine private state.

## 7. Worker pull submission is claim-bound

Operational worker execution uses a `WorkerPackage` and `CandidateEnvelope`.
SQLite enforces one active worker claim per contract. Submission validates the
claim, worker identity, fencing epoch, lease, and package binding before
projection. A failed invocation or expired lease becomes a durable terminal
fact and schedules a new recovery Contract.

## 8. Commit is transactional on the SQLite path

For SQLite-backed worker submission, Candidate, projection, accepted assets,
trace events, idempotency, terminal facts, and claim transition commit in one
database transaction. A mid-commit failure rolls the whole submission back.

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
