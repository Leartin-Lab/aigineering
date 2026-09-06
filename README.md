# Aigineering

> **Smarter boundaries, not smarter models.**

Aigineering is a zero-trust runtime for AI workers. A Worker output is a
Candidate, not shared state. Only effects that pass signature, claim,
projection, authority, allowance, and acceptance checks become durable facts.

It is not a prompt harness, a static workflow engine, or a hidden multi-agent
scheduler. Models may still produce incorrect content inside an authorized
output; Aigineering prevents unauthorized output and invisible control flow
from becoming runtime truth.

## Quick start

Python 3.11 or newer is required.

```bash
git clone https://github.com/Leartin-Lab/aigineering.git
cd aigineering
pip install -e ".[dev]"

# Configure an OpenAI-compatible LLM Worker
export AIGINEERING_API_KEY="..."
export AIGINEERING_MODEL="your-model"
export AIGINEERING_BASE_URL="https://provider.example/v1"

# Persistent execution through the normal claim/package/submit protocol
aig run "build a report with citations"

# Inspect the resulting facts and trace
aig trace
aig asset ls
```

Initialize a signed local domain before publishing tasks or assets directly:

```bash
aig domain init
aig asset add --name source --content "reviewed evidence"
aig task create \
  --name research \
  --description "Produce a report from the disclosed source." \
  --input source \
  --activation source \
  --output report
```

Mock execution is an explicit deterministic dry-run, never the production
default:

```bash
aig demo "build a report with citations" --worker mock
```

Existing agent harnesses can keep their own model and tool orchestration while
using signed pull/submit Candidates. See the
[agent harness migration reference](docs/reference/agent-harness-migration.md).

Executable AI-for-science integrations are available as installable example
skills: [`literature-evidence`](examples/literature-evidence/SKILL.md) turns
retrieval through independent review into separately testable tasks, while
[`scientific-data-profile`](examples/scientific-data-profile/SKILL.md) safely
profiles authorized CSV/TSV inputs without disclosing raw rows. Both use
zero-dependency adapters and offline replay fixtures. The
[`runtime-compiled AI4S run`](examples/ai4s/README.md) additionally exercises a
Skill-guided root task, a capability-routed local Fleet, and ordinary tool
continuations. The runtime-only acceptance path also covers staged planning,
independent `/attest`, root qualification, and SQLite reopen without relying
on the example audit driver.

## v0.5.8 scope

v0.5.8 is the stable single-machine reference release. It provides:

- actor-signed Candidate publication;
- one Candidate commitment boundary for CLI, Worker, Plugin, and HTTP surfaces;
- SQLite transactions for claims, fencing, projection, facts, trace, and
  idempotency;
- reconstructable runtime views derived from append-only records;
- claim-bound Worker packages and authenticated submissions;
- staged planning and replanning as ordinary independently claimable tasks;
- causal allowance containment for recursive publication;
- independent output attestation where a Contract requires it;
- deterministic replay, lineage, audit, and task-status views;
- same-machine active-active Worker arbitration over one SQLite domain;
- mock and OpenAI-compatible LLM Workers;
- optional FastAPI integration through the `api` extra;
- an optional Redis read projection that is disposable and reconstructable from
  SQLite;
- separate content, signed-definition, and signed-association identities with
  many-to-many history;
- v4 Contracts that bind label-selected context to exact Asset IDs;
- v5 Contracts that separate execution requirements from delegated routing
  scope;
- graph-native ordinary Worker outputs signed by the WorkerHost;
- terminal/claim fencing and durable deterministic commitment rejections;
- one causal allowance source and one terminal-fact constructor;
- planning label containment before Candidate commitment;
- explicitly configured, Contract-scoped tool registries and a separate
  ToolWorker;
- exact verifiable slice derivations and claim-gated disclosure;
- independently attested descendant outputs;
- deterministic JSON output shapes that independent review cannot override;
- executable AI4S literature and safe data-profile examples;
- heterogeneous local Worker fleets using independent SQLite connections;
- parallel tool calls compiled into ordinary tasks and a boolean join;
- durable recovery from claim-bound structural output rejection;
- executable local tool contracts with deterministic input/output schemas,
  version binding, UTF-8 output limits, and descriptor-drift rejection;
- structured tool execution metadata and a read-only `task audit --json`
  productivity projection derived from durable lineage facts;
- a runtime-only AI4S/Fleet tool-observation → continuation → independent
  verifier loop that survives SQLite reopen.

The release has deterministic boundary, reconstruction, concurrency, artifact,
and bounded real-LLM evidence. See the
[v0.5.3 convergence report](reports/053-boundary-convergence-2026-08-09.md) and
the [v0.5.4 AI4S evidence](reports/054-ai4s-auditable-example-2026-08-13.md),
the [v0.5.5 Fleet evidence](reports/055-local-worker-fleet-2026-08-14.md), and
the [v0.5.6 tool-closure evidence](reports/056-tool-closed-loop-productivity-2026-08-23.md).

Inspect the accepted asset graph without changing authoritative state:

```bash
aig graph contents --json
aig graph definitions --json
aig graph assertions --json
```

The graph is reconstructed from SQLite RuntimeRecords. Similarity adapters may
propose signed relations, but a score is neither identity nor authority.

For read-heavy CLI or API use, install and configure the optional projection:

```bash
pip install "aigineering[redis]"
export AIGINEERING_REDIS_URL="redis://127.0.0.1:6379/0"
aig cache status --json
aig cache rebuild --json
```

Redis never stores authoritative Candidates, facts, claims, allowance,
acceptance, or terminal state. If it is unavailable, supported read views fall
back to SQLite. Release evidence for this adapter is recorded in
[`reports/051-redis-query-projection-2026-07-31.md`](reports/051-redis-query-projection-2026-07-31.md).
The signed graph acceptance evidence is recorded in
[`reports/052-signed-definition-content-graph-2026-07-31.md`](reports/052-signed-definition-content-graph-2026-07-31.md).

v0.5.7 adds fail-closed tool argument and schema fixes plus historical ADR
clarifications. See [patch evidence](reports/057-tool-validation-patches-2026-09-06.md).

v0.5.8 adds backup-first reconstruction diagnostics, reproducible scaling
measurements, AST dependency checks, and broader automated release gates.
See the [release evidence guide](docs/reference/release-evidence.md) and
[v0.5.8 evidence](reports/058-reproducible-release-evidence-2026-09-06.md).

## Non-goals

v0.5.8 does not claim:

- cross-machine consensus or distributed Store semantics;
- public-network deployment hardening;
- an external security audit;
- semantic truth of model-produced content;
- scheduler fairness across independent machines;
- a generic workflow language;
- production MCP transport;
- process-level tool timeout, cancellation, or isolation;
- exactly-once delivery for external side effects;
- cross-machine Store and Worker discovery.

The optional HTTP adapter binds to the local reference runtime. Authentication,
TLS, rate limiting, and hostile-network controls remain deployment
responsibilities.

## Core boundary

```text
Contract + disclosed facts
        ↓
authenticated Worker claim
        ↓
signed Candidate effects
        ↓
projection + authority + allowance + acceptance
        ↓
atomic fact and trace commitment
```

The non-negotiable rules are documented in
[`docs/boundary-invariants.md`](docs/boundary-invariants.md). The implemented
architecture is described by [`DESIGN.md`](DESIGN.md).

## Development

Development happens on `dev`. Stable releases are promoted to `main` after the
full CI and artifact gates pass. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT — see [`LICENSE`](LICENSE).
