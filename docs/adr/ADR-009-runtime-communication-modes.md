# ADR-009: Runtime Communication Modes

**Status:** Accepted (ingress mechanics superseded by ADR-011 and Change 001)
**Date:** 2026-06-23
**Scope:** v0.5.0 delivery-blocking
**Related:** ADR-008, ADR-006, internal ADR-039, ADR-042, ADR-043

> Current implementation note (2026-07-19): all three modes publish signed
> typed Candidates through the shared commitment reducer. `RuntimeIngress` has
> been deleted; references below describe the historical consolidation step.
> `DESIGN.md` is the current implemented truth.

## Context

The CLI, nested engines, and future distributed workers need to interact
with the runtime without blurring the candidate-to-fact boundary or
leaking implementation details across domain boundaries.

The plan (`.omo/plans/050-runtime-boundary-refactor-plan.md` §2.11)
identifies three distinct communication modes. All three must use the same
Candidate protocol and commitment boundary — otherwise the system reintroduces
hidden mutation paths and makes worker substitutability false.

## Decision

The runtime supports three communication modes. v0.5.0 implements modes 1 and
2 and a same-machine SQLite reference proof for mode 3. Cross-machine Store
sharing, discovery, and consensus remain deferred.

### Mode 1: CLI Worker Mode

`aig` acts as a worker/client of the runtime's unified ingress and worker
protocol.

```
aig command → signed Candidate / worker protocol
→ task/asset declarations accepted into shared pools
→ CLI worker polls/claims next eligible package
→ CLI worker invokes execution or creates ordinary child tasks
→ CLI worker submits candidate envelope
→ the commitment boundary projects, authorizes, traces, reduces, commits
→ run exits when wait condition satisfied/failed/timed out
→ serve keeps claiming until stopped
```

Commands:
- `aig run` — single-shot CLI worker: claim, execute, submit, exit
- `aig serve` — long-lived CLI worker: keep claiming eligible work

CLI commands (`contract add`, `asset add`, `capability add`, etc.) are
actor actions against the Candidate commitment boundary — they never write
store/control state directly.

### Mode 2: Engine-to-Engine Black-Box Direction

An Engine can claim a task package, perform work, complete it, and submit
assets through the same worker protocol as any other worker.  This proves
Engine can be treated as a black-box worker and points toward
concurrent/distributed operation.

```
task package + disclosed facts
→ local/nested/remote Engine claims it like any worker
→ EngineWorker executes in an invocation-scoped inner fact domain
→ Engine submits candidate envelope / declared facts
→ shared Candidate commitment projects, authorizes, traces, reduces
```

**Visibility rule (mode 2)**: When an Engine operates as a worker across
different asset/task domains, its internal subtasks, assets, and trace
are not visible through the outer worker boundary.  The outer domain sees
only the worker candidate/effects, authorized output assets, declared
provenance, and allowed summary trace.

The reference bridge accepts an inner Store factory and persisted actor key.
It records deterministic operation and completion Assets binding outer
Contract/claim/package/epoch to the inner root and selected output Asset IDs.
A fresh EngineWorker can reconstruct and reuse accepted inner work; the outer
claim fence rejects a late result from an expired operation.

**v0.5.0 non-goal**: Full remote orchestration, transport, discovery,
and cross-domain consistency are not implemented in v0.5.0.  The
black-box proof demonstrates the architectural viability, not a
production distributed runtime.

### Mode 3: Shared-Domain Mode (Local Reference Proof)

Multiple engines/workers attach to the same asset/task domain so
subtasks, assets, and trace are ordinary shared facts, observable
according to normal disclosure, trace, and authority policies.

```
Engine A + Engine B + workers → same asset/task domain
→ visible to domain clients: all ordinary facts under normal policies
```

v0.5.0 proves same-machine active-active arbitration with independent
processes/connections over one SQLite fact domain, including fencing and
cross-replica claim/renew/submit. Cross-machine operation requires a different
Store implementation and distributed consistency design and is deferred.

### Visibility Summary

| Mode | Internal subtasks/assets/trace | Outer boundary |
|------|-------------------------------|----------------|
| CLI worker | N/A (CLI is a single worker) | Command result, accepted facts, trace authorized by connected domain |
| Engine-to-Engine (different domains) | Hidden; only authorized effects/summaries exported | Candidate/effects, authorized outputs |
| Shared-domain | Visible (same domain) | Same-machine SQLite reference; cross-machine distribution deferred |

## Consequences

### Positive

- CLI convenience and nested-engine operation use the same protocol
- Engine is substitutable with other workers at the protocol boundary
- Visibility rules prevent nested engines from leaking implementation
  details
- Future distributed deployment has a clear architecture path

### Negative

- `aig run` and `aig serve` must operate through claim/package/submit
  semantics, not direct task push (more verbose)
- Engine-as-worker requires explicit summary/export assets to cross domain
  boundaries

## Verification

```bash
# CLI worker mode: aig run uses ingress + worker protocol
pytest tests/ -k "run.*worker or run.*ingress"

# Engine-as-worker: same protocol boundary as other workers
pytest tests/ -k "engine.*claim.*protocol or engine.*worker.*boundary"

# Visibility: internal trace hidden across domains
pytest tests/ -k "engine.*visibility or internal.*trace.*hidden"

# Shared-domain mode documented as deferred
rg "shared-domain.*defer|deferred.*shared-domain" docs/adr
```
