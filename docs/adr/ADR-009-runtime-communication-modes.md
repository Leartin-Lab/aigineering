# ADR-009: Runtime Communication Modes

**Status:** Accepted
**Date:** 2026-06-23
**Scope:** v0.5.0 delivery-blocking
**Related:** ADR-008, ADR-006, internal ADR-039, ADR-042, ADR-043

## Context

The CLI, nested engines, and future distributed workers need to interact
with the runtime without blurring the candidate-to-fact boundary or
leaking implementation details across domain boundaries.

The plan (`.omo/plans/050-runtime-boundary-refactor-plan.md` §2.11)
identifies three distinct communication modes.  All three must use the
same `RuntimeIngress` (ADR-008) — otherwise the system reintroduces
hidden mutation paths and makes worker substitutability false.

## Decision

The runtime supports three communication modes.  v0.5.0 implements modes
1 and 2.  Mode 3 is documented and boundary-protected now, but
intentionally deferred.

### Mode 1: CLI Worker Mode

`aig` acts as a worker/client of Engine's unified ingress and worker
protocol.

```
aig command → Engine unified ingress / worker protocol
→ task/asset declarations accepted into shared pools
→ CLI worker polls/claims next eligible package
→ CLI worker invokes execution or creates ordinary child tasks
→ CLI worker submits candidate envelope
→ Engine projects, authorizes, traces, reduces, commits
→ run exits when wait condition satisfied/failed/timed out
→ serve keeps claiming until stopped
```

Commands:
- `aig run` — single-shot CLI worker: claim, execute, submit, exit
- `aig serve` — long-lived CLI worker: keep claiming eligible work

CLI commands (`contract add`, `asset add`, `capability add`, etc.) are
worker actions against Engine's unified ingress — they never write
store/control state directly.

### Mode 2: Engine-to-Engine Black-Box Direction

An Engine can claim a task package, perform work, complete it, and submit
assets through the same worker protocol as any other worker.  This proves
Engine can be treated as a black-box worker and points toward
concurrent/distributed operation.

```
task package + disclosed facts
→ local/nested/remote Engine claims it like any worker
→ Engine may execute directly or publish ordinary subtasks/assets
→ Engine submits candidate envelope / declared facts
→ shared RuntimeIngress projects, authorizes, traces, reduces
```

**Visibility rule (mode 2)**: When an Engine operates as a worker across
different asset/task domains, its internal subtasks, assets, and trace
are not visible through the outer worker boundary.  The outer domain sees
only the worker candidate/effects, authorized output assets, declared
provenance, and allowed summary trace.

**v0.5.0 non-goal**: Full remote orchestration, transport, discovery,
and cross-domain consistency are not implemented in v0.5.0.  The
black-box proof demonstrates the architectural viability, not a
production distributed runtime.

### Mode 3: Shared-Domain Mode (Deferred)

Multiple engines/workers attach to the same asset/task domain so
subtasks, assets, and trace are ordinary shared facts, observable
according to normal disclosure, trace, and authority policies.

```
Engine A + Engine B + workers → same asset/task domain
→ visible to domain clients: all ordinary facts under normal policies
```

**Deferred beyond v0.5.0**.  Shared-domain mode requires stronger domain
identity, visibility controls, and multi-engine consistency work.

### Visibility Summary

| Mode | Internal subtasks/assets/trace | Outer boundary |
|------|-------------------------------|----------------|
| CLI worker | N/A (CLI is a single worker) | Command result, accepted facts, trace authorized by connected domain |
| Engine-to-Engine (different domains) | Hidden; only authorized effects/summaries exported | Candidate/effects, authorized outputs |
| Shared-domain | Visible (same domain) | All ordinary facts under normal disclosure/authority policies (deferred) |

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
