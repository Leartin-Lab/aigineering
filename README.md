# Aigineering

> **Smarter boundaries, not smarter models.**

**Models may hallucinate. Aigineering prevents unauthorized outputs from becoming runtime facts.**

Aigineering is an **Agent Runtime** — infrastructure that manages agent task lifecycle, context isolation, permission control, and auditable traces. It is not a prompt harness, not a workflow engine, not "yet another agent framework."

---

## What Aigineering Does

The core insight: **worker output is a candidate, not a fact.** Only output that passes the commitment boundary — disclosure checks, declared-output authority verification — becomes a committed runtime fact. Everything else (including undeclared outputs) is rejected and recorded in the trace.

Aigineering does NOT prevent models from generating false content inside an authorized output. It prevents **unauthorized outputs** from entering shared state.

This is **Zero Trust for AI agents** translated into runtime semantics:

- **Never trust, always verify** → worker output is candidate, not fact
- **Assume breach** → worker may be injected, confused, compromised, or wrong
- **Least privilege** → disclosure is limited to declared inputs
- **Explicit trust boundaries** → commitment boundary (candidate → fact)
- **Explainability and audit** → trace completeness (including rejected candidates)

---

## Quick Start

```bash
# Clone and install (requires Python 3.11+)
git clone https://github.com/Leartin-Lab/aigineering.git
cd aigineering
pip install -e ".[dev]"

# Quick demo — run the hallucination containment scenario in memory
aig demo "build report with citations"

# Or run with persistence (creates replayable session)
aig run "build report with citations"

# Use an OpenAI-compatible LLM worker
export AIGINEERING_API_KEY="..."
aig run "build report with citations" --worker llm --model gpt-4.1-mini

# See what happened — including what was REJECTED
aig trace

# Trace lineage from output back to source
aig audit --asset-name final_report

# Replay a persisted session and validate consistency
aig session ls          # list sessions
aig replay <session_id>   # replay a session
```

---

## Status: v0.4.10 — Single-Node alpha/experimental Kernel

This release provides an experimental single-node kernel for local research and early integration work. It demonstrates the core invariant: **unauthorized worker outputs cannot become runtime facts.** The v0.4 kernel is focused on strong protocolization and single-node runtime infrastructure: immutable data models, capability containment, method-first extensibility, SQLite-backed transactional state, recoverable engine state, worker pull/submit protocol, and a modular JSON CLI.

This is not a security-audited production release. It is intended for local experiments, research prototypes, and early integration work where auditable runtime boundaries matter.

The transactional worker-submit guarantees (single active claim, idempotency, candidate envelope binding) apply to the `aig worker next` / `aig worker submit` protocol path. The `aig run` command is a local demo and direct execution path that does not exercise the full worker protocol; it is intended for quick experiments and smoke tests, not for evaluating distributed runtime safety.

**Currently implemented:**
- Deterministic SHA-256 content-addressed IDs
- Asset / Contract / Candidate / TraceEntry data models
- Candidate-to-fact boundary (declared-output authority gate + reserved-name checks)
- Rejected candidate recording in trace (with rejection reasons)
- Parse-error and duplicate-output rejection
- Mock worker demo (built-in `build_report` scenario)
- CLI: `aig demo`, `aig run`, `aig trace`, `aig audit`, `aig replay`, `aig session`
- JSONL persistent trace (`.aig/traces/`) with atomic append
- JSONL persistent asset/contract store (`.aig/store/`)
- Session manifest (`.aig/sessions/`) with trace linkage
- Full replay from persisted runtime state with consistency validation
- Label-based asset injection with traceable `label_resolved` events
- Asset disclosure policy (`promptable=False` assets remain stored but are not disclosed)
- Asset provenance metadata (`origin`, `trust_tier`, `minted_by`, `source_uri`)
- Deterministic provenance signatures (`signed_by`, `signature`)
- Replay-time verification for deterministic provenance signatures
- Worker protocol interface for candidate-producing execution environments
- Worker-produced assets use explicit worker provenance, independent of the mock demo
- OpenAI-compatible LLM worker for chat-completions endpoints
- Reusable worker prompt builder aligned with `/exec`, `/plan`, `/replan`, and `/tool`
- Structured `/exec`, `/plan`, `/replan`, and `/tool` action parser
- System method sub-contract builder for `/plan`, `/replan`, and `/tool`
- Engine scheduling for method actions without direct parent state mutation
- System method context assets and reserved-output authority for method sub-contracts
- Method scheduling deduplication by deterministic child contract identity
- Tool method execution with `_tool_call_*` and `_tool_obs_*` system assets
- Parent resume from completed method assets without output shortcutting
- CLI worker selection for mock and OpenAI-compatible LLM workers
- End-to-end LLM protocol tests for tool use and protected-output rejection
- Planner result expansion into non-system child contracts
- CLI trace rendering for method scheduling, tool execution, resume, and expansion events
- Tool registry with serializable `ToolSpec` and private handlers
- SQLite-backed asset, contract, trace, worker-claim, and idempotency state
- Transactional worker candidate submission across assets, trace, idempotency, and claim transition
- Worker package / candidate envelope protocol with claim and package binding
- Claim-bound SQLite worker submission via `aig worker next` / `aig worker submit`
- Database-enforced single active claim per contract
- Recoverable runtime state from persisted contracts, assets, and trace events
- Method-first CLI execution path for `/plan`, `/replan`, `/retry`, and `/tool`

**Not yet implemented (see ROADMAP.md):**
- MCP / Skills ecosystem adapters
- Multi-contract orchestration
- Distributed runtime across shared stores
- External security audit and deployment hardening
- PyPI release

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## Development

Development happens on `dev`. Changes to `main` should go through pull requests
after CI passes. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
