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

## Status: v0.5.0 — Local Productivity Formal Release

This release keeps the v0.4 single-node kernel constraint and adds a more
practical local productivity layer. The important change in v0.5.0 is that
the v0.5 boundary work is tighter: runtime ingress is the production mutation
gate, declared-output completion is reactive, and output satisfaction now
filters out observation/context assets.

This is not a security-audited production release. It is intended for local
experiments, research prototypes, and early integration work where auditable
runtime boundaries matter.

The transactional worker candidate submission guarantees apply to the
`aig worker next` / `aig worker submit` protocol path: worker packages are
claim-bound, submitted candidates are idempotency-bound, and accepted assets
plus trace records commit through the SQLite runtime store. The `aig run`
command remains a local direct execution path for demos and smoke tests.

**Kernel Boundaries**
- Worker output is a candidate until projected through the commitment boundary.
- Declared-output authority checks decide which fragments become facts.
- Reserved runtime namespaces are protected by default.
- Completed parent contracts are satisfied by declared asset names, including assets produced by child/continuation contracts.
- A claimed contract is never returned to "unclaimed" state; retry/recovery creates a new contract.
- Tool observations continue work by creating continuation contracts, not by reactivating the same parent task.
- Runtime recovery derives completed, suspended, budget, method scheduling, continuation context, and trace state from durable records.
- SQLite trace records persist LLM usage metadata for token/cost accounting.

**Productivity Surface**
- Control-plane asset and contract injection via `aig asset` and `aig contract`.
- Additive asset slicing, replacement claims, version lists, and lineage views.
- Behavior prompt assets via `aig behavior`.
- Tool, memory, persona, MCP, and skill descriptors as signed capability assets.
- Capability/MCP/skill/provider-config/slice/replacement changes are traceable control-plane events.
- OpenAI-compatible LLM worker with retry handling, provider capability metadata, usage metadata, and multi-tool-call support.
- Worker package / candidate envelope protocol with claim and package binding.
- CLI trace timeline, tree, and DAG views for method scheduling, continuation, tool execution, expansion, and completion.
- Replay, audit, and session commands for local persisted runs.
- Experimental `aig repl` and optional FastAPI surface via `aig serve` with the `api` extra.

**Still Explicitly Out of Scope**
- Distributed runtime across shared stores.
- External security audit and deployment hardening.
- Full self-modifying control plane where every policy/config item is an authorized asset update.
- Multi-node lease recovery, queueing, and scheduler fairness guarantees.

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## Development

Development happens on `dev`. Changes to `main` should go through pull requests
after CI passes. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
