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

# Deterministic local demonstration
aig demo "build a report with citations"

# Persistent execution through the normal claim/package/submit protocol
aig run "build a report with citations" --worker mock

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

An OpenAI-compatible Worker can be used without changing the runtime protocol:

```bash
export AIGINEERING_API_KEY="..."
aig run "build a report with citations" \
  --worker llm \
  --model your-model \
  --base-url https://provider.example/v1
```

## v0.5.1 scope

v0.5.1 is the stable single-machine reference release. It provides:

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
- optional FastAPI integration through the `api` extra.
- an optional Redis read projection that is disposable and reconstructable from
  SQLite.

The release has deterministic boundary, reconstruction, concurrency, artifact,
and real-LLM acceptance evidence. See
[`reports/050-post-review-boundary-hardening-2026-07-19.md`](reports/050-post-review-boundary-hardening-2026-07-19.md).

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

## Non-goals

v0.5.1 does not claim:

- cross-machine consensus or distributed Store semantics;
- public-network deployment hardening;
- an external security audit;
- semantic truth of model-produced content;
- scheduler fairness across independent machines;
- a generic workflow language.

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
