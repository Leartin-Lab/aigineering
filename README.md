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

# See what happened — including what was REJECTED
aig trace

# Trace lineage from output back to source
aig audit --asset-name final_report

# Replay a persisted session and validate consistency
aig session ls          # list sessions
aig replay <session_id>   # replay a session
```

---

## Status: Pre-Alpha

This is an early proof-of-concept demonstrating the core invariant: **undeclared outputs cannot become runtime facts.**

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
- Worker protocol interface for candidate-producing execution environments
- Worker-produced assets use explicit worker provenance, independent of the mock demo
- OpenAI-compatible LLM worker for chat-completions endpoints

**Not yet implemented (see ROADMAP.md):**
- MCP / Skills / Tools
- Multi-contract orchestration
- Distributed runtime
- PyPI release

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## Development

Development happens on `dev`. Changes to `main` should go through pull requests
after CI passes. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
