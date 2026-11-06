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

The inner Store may also be an already initialized local workspace. An explicit
Fleet profile pins its canonical directory, Genesis, receiving root key and
receiver Fleet. This mode assumes one trusted operator controls both domains;
it does not establish cross-organization authorization or confidentiality.
Each domain retains its own signed fact authority and locally authorized budget.
The outer policy still decides whether returned output satisfies its obligation.

Each operation binds source/target identity, exact outer claim and receiver
configuration. Imported inputs use receiving-domain IDs. A new operation uses
a distinct inner root with frozen contract-tree output provenance; same-name
historical Assets do not fulfill it. Exact replay cannot refresh configuration
recorded intent. The trusted operator must not concurrently launch different
receiver configurations for the same outer claim: the bounded history precheck
is not a new transactional cross-domain intent arbiter.
The fingerprint includes declarative Fleet/tool metadata; referenced Python
factories, handlers and dependencies must remain fixed by the operator. It is
not a code attestation mechanism.
Existing canonical registration and runtime maintenance remain in use;
root-scoped invocation does not imply root-scoped maintenance. Receiver Fleets
reject Engine adapters, preventing recursive configuration expansion.

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
