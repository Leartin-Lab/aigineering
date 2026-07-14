# Aigineering Roadmap

## Current Status

**Version:** `v0.5.0` (single-machine formal release)

This is not a security-audited production release. The formal designation
refers to the supported single-machine local-productivity scope only.

Aigineering is a Zero Trust Agent Runtime. The current milestone is the **050
local productivity formal release**: a traceable CLI/control-plane surface on top of the
v0.4 single-node kernel, with runtime ingress as the production mutation gate,
reactive declared-output completion, and output satisfaction that filters out
observation/context assets.

The v0.4 kernel provides SQLite-backed transactional submission, recoverable
runtime state, capability containment, and a worker pull/submit protocol. The
transactional candidate submission guarantees apply through the `aig worker
next` / `aig worker submit` protocol path: worker packages are claim-bound,
submitted candidates are idempotency-bound, and accepted assets plus trace
records commit atomically through the SQLite runtime store.

**What's working today:**
- Candidate-to-fact boundary with declared-output authority checks
- SQLite-backed durable store with schema versioning and migration
- Worker claim/idempotency tables with single-active-claim enforcement
- Resumable engine state from persisted assets, contracts, traces
- OpenAI-compatible LLM worker with retry, usage metadata, multi-tool-call
- Control-plane asset/contract/behavior injection via CLI
- Additive asset slicing, replacement claims, version lists, lineage views
- Capability, MCP, skill, tool, memory, and persona descriptors as signed assets
- Stable MCP function call to method/tool contract expansion
- Experimental REPL (`aig repl`) and optional FastAPI surface (`aig serve`)
- CLI trace timeline, tree, and DAG views
- Replay, audit, and session commands for persisted runs

**What's not done yet:**
- PyPI publish (API stability gate)
- Cryptographic signing enabled as the default runtime policy
- Universal trust-policy enforcement over signer, origin, trust tier, labels
- Distributed runtime across shared stores
- External security audit and deployment hardening
- Crash-injection and concurrent-worker stress tests

```text
Contract -> Worker/Sub-agent -> Candidate -> Projection/Method -> Asset/Trace
```

The project has completed Waves 0–5 of its development plan. It is not
externally audited or deployment-hardened.

## v0.1 - Hallucination Containment MVP

- [x] Deterministic SHA-256 content-addressed IDs
- [x] Asset / Contract / Candidate / TraceEntry data models
- [x] Candidate-to-fact boundary
- [x] Declared-output authority gate
- [x] Reserved runtime name rejection
- [x] Rejected candidate recording in trace
- [x] Mock worker demo
- [x] `aig run`, `aig trace`, `aig audit` CLI

## v0.2 - Persistence, Replay, and Provenance

- [x] JSONL persistent trace (`.aig/traces/session_*.jsonl`)
- [x] Persistent asset/contract store (`.aig/store/*.jsonl`)
- [x] Session manifest (`.aig/sessions/*.json`)
- [x] `aig replay` from persisted sessions
- [x] Projection/commit separation with pure `ProjectionResult`
- [x] Structured rejection categories and projection status
- [x] Boundary regression pack
- [x] Label-based asset injection with placeholder assets
- [x] Asset disclosure policy for non-promptable assets
- [x] Asset provenance metadata (`origin`, `trust_tier`, `minted_by`, `source_uri`)
- [x] Deterministic provenance signatures (`signed_by`, `signature`)
- [x] Replay-time verification for deterministic provenance signatures
- [x] Worker protocol interface
- [x] Worker-origin provenance for projected assets

## v0.3 - Real Worker and Structured Protocol

- [x] OpenAI-compatible LLM worker
- [x] CLI worker selection for mock and LLM workers
- [x] Prompt builder aligned with structured actions
- [x] `/exec` / `/plan` / `/replan` / `/tool` parsing
- [x] Method actions as system sub-contracts
- [x] Method context assets for scheduled sub-contracts
- [x] System authority for declared reserved method outputs
- [x] Tool execution through `_tool_call_*` and `_tool_obs_*` assets
- [x] Parent resume from method observations
- [x] Planner result expansion into non-system child contracts
- [x] End-to-end fake-LLM protocol boundary tests
- [x] CLI trace rendering for method scheduling, tool execution, resume, and expansion

## v0.4 - Kernel Infrastructure

Focus: make the single-node runtime durable, resumable, protocolized, and safer.

- [x] SQLiteStore or equivalent single-file durable store
- [x] Schema-versioned SQLite substrate with v1 -> v2 migration
- [x] Contract authority metadata persistence (`minting_authority`, `sensitive_input_policy`)
- [x] SQLite trace store operations for replay and recovery
- [x] Worker claim and idempotency tables
- [x] Transactional candidate submission across accepted assets, trace, idempotency, and claim transition
- [x] Database-enforced single active worker claim per contract
- [x] Worker package and candidate envelope claim/package binding
- [x] Claim-bound SQLite worker submission via `aig worker next` / `aig worker submit`
- [x] Resumable engine state for completed/suspended contracts, budgets, and method context
- [x] Crash recovery from persisted assets/contracts/traces/session manifest
- [x] `aig trace --tree` / `aig trace --dag` as views, not runtime truth
- [x] CLI split into smaller command modules
- [x] CLI default method registry for method-first `/plan`, `/replan`, `/retry`, and `/tool`
- [x] 040 gate test suite for boundary, persistence, recovery, claim, and public-claim checks
- [x] Release packaging and distribution checks

### Deferred to v0.4.x

- [ ] Real cryptographic signer/verifier interface
- [ ] Trust policy over signer, origin, trust tier, labels, tool scope, and reserved prefixes
- [ ] Broader crash-injection and concurrent-worker stress tests

## v0.5 - Local Productivity Formal Release

Focus: make the single-node runtime useful for local work without weakening the
candidate/fact boundary.

- [x] Control-plane asset injection (`aig asset add/list/show`)
- [x] Control-plane contract/task injection (`aig contract add/list/show/run`)
- [x] Asset slicing, replacement claims, versions, and lineage views
- [x] Behavior prompt assets (`aig behavior add/list/show`)
- [x] LLM worker retry, provider capabilities, usage metadata, and multi-tool-call envelope
- [x] Experimental REPL (`aig repl`)
- [x] Optional experimental API/server surface (`aig serve`, `api` extra)
- [x] Stable MCP function call -> method/tool contract expansion
- [x] Stable MCP descriptor assets (`aig mcp add/list/show`)
- [x] Stable skill loading as assets (`aig skill load/list`)
- [x] Stable label-injected skill assets
- [x] Capability assets for tools, MCP, memory, and persona modules
- [ ] PyPI publish after API stabilizes

## v0.6 - Asset Management and Evaluation

- [ ] Semantic asset catalog
- [ ] Prefix search and tag filtering
- [ ] Lineage bundles
- [ ] GC: audit closure, keep flags, reflog, tombstones
- [x] Replacement claims instead of asset mutation
- [ ] Real-world LLM benchmarks
- [ ] Reproducible benchmark suite for output quality evaluation

## v0.7+ - Distributed and Production Hardening

- [ ] Full-hash distributed identity
- [ ] Worker registry and heartbeat
- [ ] Worker leases and stale-worker detection
- [ ] Concurrent execution and transactional store guarantees
- [ ] Fuzz tests for protocol, authority, and replay
- [ ] Deployment docs and security model

## Non-Goals for Early Releases

- A generic prompt harness
- A static DAG workflow engine
- A hidden multi-agent swarm scheduler
- Direct mutation of runtime facts by workers, tools, or sub-agents
