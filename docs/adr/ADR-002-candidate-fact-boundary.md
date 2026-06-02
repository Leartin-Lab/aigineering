# ADR-002: Candidate-Fact Boundary

**Status**: Accepted  
**Date**: 2025-06-02

## Context

LLM-based workers can hallucinate — producing outputs that were never declared, fabricating citations, or generating content outside the expected schema. In traditional agent systems, worker output directly becomes shared state, making hallucinated content indistinguishable from legitimate output.

## Decision

Aigineering introduces an explicit **candidate-fact boundary**: worker output is a _candidate_, not a fact. Only output that passes the commitment boundary — disclosure checks, authority verification — becomes a committed runtime _fact_. All rejected candidates are recorded in the trace.

## Consequences

- Hallucinated undeclared outputs cannot become runtime facts
- Every state transition is auditable: what was rejected, why, by which authority rule
- The trace contains uniquely rich information: not just what happened, but what was attempted and rejected
