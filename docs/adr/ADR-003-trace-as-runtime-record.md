# ADR-003: Trace as Runtime Record

**Status**: Accepted  
**Date**: 2026-06-02

## Context

Most agent systems produce logs as an afterthought — debugging information written alongside the actual execution. These logs record "what happened" but not "what was rejected" or "why a state transition was denied."

## Decision

In Aigineering, the trace is the **runtime record itself**, not a log. Each step in the ACM boundary loop — activation, disclosure, candidate projection, authority decision, completion — produces a TraceEntry. The trace is append-only and contains both accepted and rejected fragments with authority results.

## Consequences

- Trace completeness: the trace IS the execution, not a representation of it
- Replay becomes possible when trace completeness is maintained; the v0.1 MVP
  records the boundary, while full replay is planned for a later milestone
- Audit becomes first-class: reverse lineage from any asset to its origin
- Other systems say "this happened" — ACM says "this happened, that was attempted and rejected, and here's why"
