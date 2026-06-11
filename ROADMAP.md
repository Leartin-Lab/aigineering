# Aigineering Roadmap

## Status

Aigineering is a pre-alpha Zero Trust Agent Runtime. The current `v0.4.0`
milestone delivers "Strong Protocolization + Partial Infrastructure":
a protocolized, method-first, single-node runtime with transactional
durability, recoverable state, and capability containment.

```text
Contract -> Worker/Sub-agent -> Candidate -> Projection/Method -> Asset/Trace
```

The project has completed Waves 0–3 of its development plan and is ready for
broader single-machine experiments. It is not production-ready yet.

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

## v0.4 - Production Foundation

Focus: make the single-node runtime durable, resumable, and safer.

- [x] SQLiteStore or equivalent single-file durable store
- [x] Resumable engine state for completed/suspended contracts, budgets, and method context
- [x] Crash recovery from persisted assets/contracts/traces/session manifest
- [x] `aig trace --tree` / `aig trace --dag` as views, not runtime truth
- [x] CLI split into smaller command modules

### Deferred to v0.4.x

- [ ] Real cryptographic signer/verifier interface
- [ ] Trust policy over signer, origin, trust tier, labels, tool scope, and reserved prefixes

## v0.5 - Ecosystem Integration

Focus: connect the runtime to agent/tool ecosystems without weakening the boundary.

- [ ] MCP function call -> method/tool contract expansion
- [ ] MCP descriptor assets
- [ ] Skill loading as assets
- [ ] Label-injected skill assets
- [ ] Capability assets for tools, MCP, memory, and persona modules
- [ ] Interactive REPL (`aig repl`)
- [ ] API/server surface over core runtime
- [ ] PyPI publish after API stabilizes

## v0.6 - Asset Management and Evaluation

- [ ] Semantic asset catalog
- [ ] Prefix search and tag filtering
- [ ] Lineage bundles
- [ ] GC: audit closure, keep flags, reflog, tombstones
- [ ] Replacement claims instead of asset mutation
- [ ] Real-world LLM benchmarks
- [ ] AEST-style benchmark suite

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
