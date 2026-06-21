# Aigineering Whitepaper

## Asset-Causal Machines: A Runtime Boundary for Agentic Systems

This document is intentionally short for the v0.5.0-alpha.1 local productivity alpha. The
current implementation demonstrates one core invariant:

> Models may hallucinate. The runtime does not have to believe them.

Aigineering treats every worker output as a candidate. A candidate becomes a
runtime fact only after it passes a commitment boundary: disclosure, authority,
budget, and trace. In the v0.4 kernel, that boundary is backed by immutable
protocol objects, method subtasks, SQLite transactional submission, worker
claim binding, and recoverable trace state.

The runtime records both accepted and rejected fragments. This means failures are
not hidden in logs or prompt history. They are part of the execution record.

The current release is for local research and early integrations. Future
versions will extend this boundary to ecosystem adapters, external signing
policies, larger crash/concurrency test matrices, distributed stores, and
deployment hardening.
