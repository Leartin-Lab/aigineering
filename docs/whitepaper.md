# Aigineering Whitepaper

## Asset-Causal Machines: a runtime boundary for AI work

Models may hallucinate. The runtime does not have to believe them.

Aigineering treats every Worker output as a signed Candidate rather than shared
state. Projection, authority, causal allowance, acceptance policy, and one
transactional commitment boundary decide which effects become durable facts.
Rejected effects and failed attempts remain visible in the same replayable
record.

Contracts are immutable obligations over declared inputs and outputs. Workers
are stateless keyed actors. Planning, replanning, recovery, tool use, and
verification publish ordinary work rather than entering a hidden controller
stack. Task status is reconstructed from facts, so another process can reopen
the database and continue.

SQLite is the reference source of truth. The optional Redis layer is
only a disposable read projection: it can accelerate exact recall and task
views, but it cannot authorize or commit anything. Flushing Redis and rebuilding
from SQLite must reproduce the same query view.

Content, meaning, and authority are not collapsed into one identifier. A pure
content object may be associated with multiple independently signed
definitions, and a definition may acquire multiple content versions through
separately signed assertions. Semantic similarity can propose an association,
but only the normal Candidate boundary can accept it as a fact.

The current release is a single-machine reference implementation. It does not
claim external security audit, distributed consensus, hostile-network
hardening, or semantic truth of authorized model content.
