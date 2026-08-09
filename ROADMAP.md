# Aigineering Roadmap

## Current release

Version: **v0.5.3**

v0.5.3 is the stable single-machine reference release. “Stable” applies to the
documented local runtime and protocol surface; it is not a claim of external
security audit or public-network deployment hardening.

Implemented:

- one actor-signed Candidate commitment boundary;
- SQLite-backed atomic claims, fencing, submission, facts, and trace;
- append-only runtime records and deterministic materialization rebuild;
- stateless Worker pull/package/submit protocol;
- staged planning and replanning as ordinary tasks;
- recursive causal allowance containment;
- independent output attestation;
- same-machine active-active Worker arbitration;
- Engine-as-Worker isolation and restart;
- CLI audit, replay, lineage, recovery, and task projections;
- mock and OpenAI-compatible LLM Workers;
- optional FastAPI transport;
- language-neutral signed protocol conformance vectors;
- optional Redis query projection with revision fencing, rebuild, and SQLite
  fallback;
- a signed many-to-many definition/content graph with legacy reconstruction;
- exact label-context binding in v4 Contract identity;
- advisory semantic relation publication through the Candidate boundary;
- ordinary Worker output publication as an atomic signed content,
  definition, and assertion graph;
- transactional terminal/claim closure and durable deterministic commitment
  rejection facts;
- causal allowance as the sole runtime budget source;
- one terminal-fact construction owner and Plugin-owned planning scaffold;
- planning label containment aligned across prompt, Plugin, and commitment.

Release evidence is recorded in
`reports/050-post-review-boundary-hardening-2026-07-19.md` and
`reports/051-redis-query-projection-2026-07-31.md`, and
`reports/052-signed-definition-content-graph-2026-07-31.md`. v0.5.3 convergence
evidence is in `reports/053-boundary-convergence-2026-08-09.md`.

## Release gates

Every stable release must pass:

1. candidate/fact, authority, disclosure, claim, and terminal regression tests;
2. Memory/SQLite conformance and crash-atomicity tests;
3. materialization deletion and rebuild with matching semantic digest;
4. concurrent Worker and stale-claim fencing tests;
5. canonical signed JSON and protocol-vector verification;
6. Ruff check and format;
7. full deterministic test suite;
8. wheel and sdist build plus metadata validation;
9. installation-state CLI and database-reopen smoke tests;
10. bounded real-LLM system, Worker, and end-to-end scenarios.

## Candidate directions

Future candidate directions include:

- cross-machine Store and Worker discovery;
- deployment security profiles;
- reproducible productivity and quality benchmarks.

Work is not part of the supported release until its design, migration, tests,
reconstruction proof, and public evidence are complete.

## Non-goals

Aigineering is not intended to become:

- a generic prompt harness;
- a static DAG workflow engine;
- a hidden multi-agent swarm scheduler;
- a mutable conversational state container;
- a system in which Workers or tools directly write runtime facts;
- a system whose correctness depends on one Engine process remaining alive.
