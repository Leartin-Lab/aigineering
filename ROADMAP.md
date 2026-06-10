# Aigineering Roadmap

## v0.1 — Hallucination Containment MVP

- [x] Deterministic SHA-256 content-addressed IDs
- [x] Asset / Contract / Candidate / TraceEntry data models
- [x] Candidate-to-fact boundary (authority gate)
- [x] Rejected candidate recording in trace
- [x] Mock worker demo
- [x] `aig run`, `aig trace`, `aig audit` CLI

## v0.2 — Persistence & Replay (Current)

- [x] JSONL persistent trace (`.aig/traces/session_*.jsonl`)
- [x] Atomic append and reload for trace entries
- [x] `aig trace` reads latest persisted trace
- [x] `aig audit` resolves accepted asset names from persisted projection trace
- [x] Projection/commit separation with pure `ProjectionResult`
- [x] Structured rejection categories and projection status
- [x] Boundary regression pack
- [x] Persistent asset/contract store (`.aig/store/*.jsonl`)
- [x] Session manifest (`.aig/sessions/*.json`)
- [x] JSONL trace reload beyond latest-session CLI reads via `--session`
- [x] `aig replay` (from persisted session + trace)
- [x] Label-based asset injection with placeholder assets
- [x] Asset disclosure policy for non-promptable assets
- [x] Asset provenance metadata (`origin`, `trust_tier`, `minted_by`, `source_uri`)
- [x] Deterministic provenance signatures (`signed_by`, `signature`)
- [x] Worker protocol interface
- [x] Worker-origin provenance for projected assets
- [ ] SQLiteStore (single-file persistence)
- [ ] `aig retry --contract` (incremental retry)
- [ ] `aig trace --tree` / `aig trace --dag`

## v0.3 — Real LLM & Protocol

- [x] OpenAI-compatible LLM worker
- [x] CLI worker selection for mock and LLM workers
- [x] Prompt builder
- [x] `/exec` / `/plan` / `/replan` / `/tool` protocol parsing
- [x] Method action to system sub-contract semantics
- [x] Engine method sub-contract scheduling
- [x] Method context assets for scheduled sub-contracts
- [x] System authority for declared reserved method outputs
- [x] Tool method execution with call and observation assets
- [x] Parent resume from method observations
- [x] End-to-end LLM protocol boundary tests
- [x] Contract expansion for planner outputs

## v0.4 — MCP, Skills, GC, PyPI

- [x] Tool registry with serializable specs and private handlers
- [ ] MCP function call → contract expansion
- [ ] Skill loading
- [ ] Label-injected skill assets
- [ ] GC: audit closure + reflog + tombstone
- [ ] PyPI publish
- [ ] Interactive REPL (`aig repl`)

## v0.5+ — Distributed & Production

- [ ] Distributed runtime (multi-node)
- [ ] Capability assets (skills, MCP, memory, SOUL)
- [ ] Real-world LLM benchmarks
- [ ] Production hardening
