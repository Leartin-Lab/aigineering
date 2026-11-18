# Change 133: CLI guidance and runtime contraction

Status: Implemented on dev. Stable release qualification
is separate from this source change.

Review requests now check available allowance before publication and explain
the independent report identity and policy-bound decision identity. Accepted
exact replays still pass through current authentication and canonical commitment;
the balance preflight cannot authorize replay. Text-edit failures explain no-op
and terminal cases, and point to inspection or the published successor.

`aig trace --contract ID` reads durable SQLite trace when no explicit session is
selected. `--session` retains JSONL session inspection. Missing-session asset
audit exits unsuccessfully. Current truncation failures identify known effective
limits and the corresponding configuration/recovery steps. Dependency-free run
option conflicts are checked before required Redis preflight.

Internal consolidation removes the separate snapshot assembly module, merges
allowance return rules into the allowance owner, and gives aggregate queries one
frozen context/reader. Writer revalidation, exact funding proofs, immutable facts,
claim fencing and independent acceptance retain their existing owners and rules.

## Python import migration

These removals affect callers of internal Python modules. CLI commands, signed
wire identities, database schema and historical SQLite facts are preserved.

| Removed path | Current entry point |
| --- | --- |
| `core.claims.Claim` / `ClaimStore`, `core.heartbeat.Heartbeat` / `HeartbeatStore` | Canonical signed WorkerHost/Harness claim and renewal; the old in-memory presence API is retired. |
| `core.store.JsonLStore`, `adapters.legacy_jsonl.JsonLStore` | SQLiteStore for durable work; `aig replay` retains read-only legacy Asset/Contract JSONL inspection. |
| `core.methods` | `plugins.task_semantics` |
| `core.provider_config.create_provider_config_snapshot` | `core.capability_descriptors.create_provider_config_snapshot` |
| `core.allowance_returns` | `core.causal_allowance` |
| `core.commit_snapshot` | Pure comparison in `core.candidate_decision`; writer orchestration in `core.commitment`. |

Paths in this table are relative to `aigineering`. Stable `core.ids` and
`core.signing` aliases, MemoryStore reference behavior and JsonLTraceStore session
traces remain supported. Legacy JSONL keeps its two-file decoding and original
defaults/error ordering; it is not a writable runtime backend.

Behavior checks cover review replay and revoked keys, rejected relay results,
edit guidance, durable trace selection, truncation guidance, legacy decoding,
allowance conservation, writer conflicts, frozen queries and runtime recovery.
Fixture execution does not establish general model reliability or provider cost.
