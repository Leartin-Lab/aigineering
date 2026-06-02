# Aigineering Roadmap

## v0.1 — Hallucination Containment MVP (Current)

- [x] Deterministic SHA-256 content-addressed IDs
- [x] Asset / Contract / Candidate / TraceEntry data models
- [x] Candidate-to-fact boundary (authority gate)
- [x] Rejected candidate recording in trace
- [x] Mock worker demo
- [x] `aig run`, `aig trace`, `aig audit` CLI

## v0.2 — Persistence & Replay

- [ ] SQLiteStore (single-file persistence)
- [ ] JSONL trace export/import
- [ ] `aig replay` (from trace store)
- [ ] `aig retry --contract` (incremental retry)
- [ ] `aig trace --tree` / `aig trace --dag`

## v0.3 — Real LLM & Protocol

- [ ] OpenAI-compatible LLM worker
- [ ] `/exec` / `/plan` / `/replan` / `/tool` protocol parsing
- [ ] Prompt builder
- [ ] Method subtask semantics
- [ ] Contract expansion for tool calls

## v0.4 — MCP, Skills, GC, PyPI

- [ ] MCP function call → contract expansion
- [ ] Skill loading
- [ ] Label/context injection
- [ ] GC: audit closure + reflog + tombstone
- [ ] PyPI publish
- [ ] Interactive REPL (`aig repl`)

## v0.5+ — Distributed & Production

- [ ] Distributed runtime (multi-node)
- [ ] Capability assets (skills, MCP, memory, SOUL)
- [ ] Real-world LLM benchmarks
- [ ] Production hardening
