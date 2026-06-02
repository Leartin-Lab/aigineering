# Aigineering Whitepaper

## Asset-Causal Machines: A Runtime Boundary for Agentic Systems

This document is intentionally short for the pre-alpha release. The current
implementation demonstrates one core invariant:

> Models may hallucinate. The runtime does not have to believe them.

Aigineering treats every worker output as a candidate. A candidate becomes a
runtime fact only after it passes a commitment boundary: disclosure, authority,
budget, and trace. In the v0.1 MVP, the boundary proves a narrow but important
property: hallucinated undeclared outputs cannot become committed runtime facts.

The runtime records both accepted and rejected fragments. This means failures are
not hidden in logs or prompt history. They are part of the execution record.

Future versions will extend this boundary to method subtasks, tool observations,
asset slices, signatures, replay, and persistent stores.
