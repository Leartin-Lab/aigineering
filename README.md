# Aigineering

> **Smarter boundaries, not smarter models.**

**Models may hallucinate. Aigineering makes sure the runtime does not have to believe them.**

Aigineering is an **Agent Runtime** — infrastructure that manages agent task lifecycle, context isolation, permission control, resource constraints, and auditable traces. It is not a prompt harness, not a workflow engine, not "yet another agent framework."

---

## What Aigineering Does

The core insight: **worker output is a candidate, not a fact.** Only output that passes the commitment boundary — disclosure checks, authority verification, budget accounting — becomes a committed runtime fact. Everything else (including hallucinations) is rejected and recorded in the trace.

This is **Zero Trust for AI agents** translated into runtime semantics:

- **Never trust, always verify** → worker output is candidate, not fact
- **Assume breach** → worker may be injected, confused, compromised, or wrong
- **Least privilege** → disclosure, authority, and tool scope
- **Explicit trust boundaries** → commitment boundary (candidate → fact)
- **Explainability and audit** → trace completeness (including rejected candidates)

---

## Quick Start

```bash
# Clone and install
git clone https://github.com/aigineering/aigineering.git
cd aigineering
uv sync

# Run the hallucination containment demo
uv run aig run "build report with citations" --mock

# See what happened — including what was REJECTED
uv run aig trace

# Trace lineage from output back to source
uv run aig audit --asset final_report
```

---

## Status: Pre-Alpha

This is an early proof-of-concept demonstrating the core invariant: **hallucinated undeclared outputs cannot become runtime facts.**

**Currently implemented:**
- Deterministic SHA-256 content-addressed IDs
- Asset / Contract / Candidate / TraceEntry data models
- Candidate-to-fact boundary (authority gate)
- Rejected candidate recording in trace
- Mock worker demo

**Not yet implemented:**
- Real LLM integration
- MCP / Skill / Tool support
- Distributed runtime
- Garbage collection
- PyPI production release

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## License

MIT — see [LICENSE](LICENSE).
