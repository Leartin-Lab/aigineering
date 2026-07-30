# ADR-009: Runtime communication modes

Status: Accepted
Date: 2026-06-23
Related: ADR-006, ADR-008, ADR-011

## Context

CLI Workers, nested Engines, and Workers sharing one fact domain need explicit
visibility rules without weakening the Candidate boundary.

## Decision

The runtime supports three communication modes.

### CLI Worker

CLI commands publish signed Candidates or use the claim/package/submit Worker
protocol. Convenience commands do not write Store facts directly.

### Engine as Worker

An Engine-backed Worker receives an outer WorkerPackage, performs work in an
isolated inner fact domain, and exports only authorized Candidate effects.
Inner tasks, Assets, and trace remain private to the inner domain.

Bridge operations bind the outer Contract, claim, package, and epoch to the
inner task and selected output Asset IDs. A restarted EngineWorker can reopen
the inner Store; an expired outer claim cannot submit a late result.

### Shared fact domain

Multiple same-machine Workers may attach to one SQLite domain through separate
connections. Contracts, Assets, trace, and claims are shared according to the
normal disclosure and authority rules. SQLite transactions and claim epochs
arbitrate concurrent work.

Cross-machine Store consistency, discovery, and consensus are not part of
v0.5.0.

## Visibility

| Mode | Visible in outer domain |
| --- | --- |
| CLI Worker | committed effects and authorized trace |
| Engine as Worker | exported effects and bridge evidence |
| Shared domain | all ordinary facts allowed by domain policy |

## Consequences

- every mode uses the same Candidate and Worker protocol;
- nested computation does not leak private inner state;
- same-machine active-active behavior is testable without process identity;
- distributed deployment requires a Store with separately defined consistency
  semantics.
