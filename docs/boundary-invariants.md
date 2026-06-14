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
contracts. The parent task is suspended while the method contract produces a
method asset. Method handlers operate through `MethodRuntime`, not direct
Engine private state.

## 7. Worker pull submission is claim-bound

Operational worker execution uses a `WorkerPackage` and `CandidateEnvelope`.
SQLite enforces one active worker claim per contract. Submission validates the
claim, worker identity, lease, and package binding before projection.

## 8. Commit is transactional on the SQLite path

For SQLite-backed worker submission, accepted assets, trace events, idempotency,
and claim transition commit in one database transaction. A mid-commit failure
rolls the whole submission back.
