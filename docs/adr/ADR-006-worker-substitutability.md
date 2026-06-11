# ADR-006: Worker Substitutability

**Status**: Accepted
**Date**: 2026-06-11

## Context

Aigineering tasks may be executed by different kinds of workers: mock workers,
LLM workers, tool workers, script workers, human workers, MCP workers, or remote
workers. If each worker type defines its own completion and state-mutation
semantics, the runtime becomes a collection of special cases.

The runtime needs one execution boundary that all workers satisfy.

## Decision

Every worker receives a contract plus disclosed assets and returns a candidate.
The runtime, not the worker, decides which candidate effects become committed
assets.

```text
contract + disclosed assets
-> worker
-> candidate
-> projection
-> authority
-> committed assets + trace
```

Worker internals are opaque. An LLM may deliberate through tokens, a tool worker
may call a local function, a human worker may use a review UI, and a remote
worker may execute elsewhere. Those differences do not change the boundary.

Workers must not directly mutate the store, mark tasks complete, grant
authority, or commit runtime facts.

## Consequences

- Mock, LLM, tool, script, human, MCP, and remote workers can share the same
  contract semantics.
- Tests against mock workers exercise the same candidate-fact boundary as real
  workers.
- Distributed execution becomes a deployment form of the worker boundary, not a
  second runtime model.
- Worker-specific adapters can evolve without weakening projection, authority,
  replay, or audit.
