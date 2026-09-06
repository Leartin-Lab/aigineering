# Aigineering Roadmap

## Current release

Version: **v0.5.8**

v0.5.8 is the stable single-machine reference release. “Stable” applies to the
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
- planning label containment aligned across prompt, Plugin, and commitment;
- a framework-neutral signed Candidate adapter for existing agent harnesses;
- configured LLM execution as the CLI default, with mock explicit for tests;
- explicitly configured, Contract-scoped local tool registries with separate
  capability-routed ToolWorkers;
- verifiable exact slice derivations and claim-gated disclosure;
- executable AI4S literature and data-profile examples with offline replay;
- descendant-aware independent acceptance for delegated output Assets;
- deterministic JSON output shapes inherited by planned and recovery work;
- Contract-v5 separation of execution requirements from delegation scope;
- capability- and pool-routed heterogeneous local Worker fleets;
- parallel tool calls compiled into ordinary tasks plus a boolean join;
- commit-time completion convergence across concurrent SQLite writers;
- durable recovery for both projection and claim-bound structural rejection;
- a runtime-compiled AI4S example driven by one root task and a Skill;
- executable local tool contracts with deterministic input/output schemas,
  version binding, UTF-8 output limits, and descriptor-drift rejection;
- structured tool execution metadata for duration, result bytes, error type,
  and retryability;
- a read-only `task audit --json` productivity projection reconstructed from
  Contract lineage and durable runtime records;
- a runtime-only AI4S/Fleet acceptance loop covering staged planning, tool
  observation, continuation, independent `/attest`, root qualification, and
  SQLite reopen.

Release evidence is recorded in
`reports/050-post-review-boundary-hardening-2026-07-19.md` and
`reports/051-redis-query-projection-2026-07-31.md`, and
`reports/052-signed-definition-content-graph-2026-07-31.md`. v0.5.3 convergence
and harness stabilization evidence is in
`reports/053-boundary-convergence-2026-08-09.md`.
v0.5.4 AI4S and derivation evidence is recorded in
`reports/054-ai4s-auditable-example-2026-08-13.md`.
Prior v0.5.5 fleet and runtime-compilation evidence is recorded in
`reports/055-local-worker-fleet-2026-08-14.md`.
The v0.5.6 implemented boundary is described in
`changes/014-tool-closed-loop-productivity.md` and
`docs/adr/ADR-020-tool-closed-loop-productivity.md`.
Tool-closure, reconstruction, and bounded live Fleet evidence is recorded in
`reports/056-tool-closed-loop-productivity-2026-08-23.md`.

v0.5.7 adds bounded tool argument/schema validation fixes and clarifies historical
ADR implementation ownership. See `changes/015-tool-validation-patches.md` and
`reports/057-tool-validation-patches-2026-09-06.md`.

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

## v0.5.8 evidence and operability

Implemented in `changes/016-reproducible-release-evidence.md` and ADR-021:

- backup-first reconstruction verification with retained mismatch evidence;
- reproducible signed-publication scaling measurements;
- Python 3.11–3.13 and API/Redis CI coverage plus installed-wheel checks;
- publication reuse of validated artifacts;
- AST-based dependency guards supplementing existing architecture tests.

Local observations and remote-execution limits are recorded in
`reports/058-reproducible-release-evidence-2026-09-06.md`.
The v0.5.6 diagnostic rebuild mismatch remains unexplained. Context-loading
optimization, process isolation, production MCP, and external side-effect
guarantees remain future work.

## Candidate directions

Future candidate directions include:

- production MCP transport and protocol integration;
- process-level tool timeout, cancellation, and isolation;
- exactly-once coordination for external side effects;
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

The v0.5.6 local tool loop does not claim production MCP transport,
process-level timeout/cancellation/isolation, exactly-once external side
effects, or cross-machine discovery.
