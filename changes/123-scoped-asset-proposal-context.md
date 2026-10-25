# Change 123: Select exact Asset-proposal projection inputs

## Problem

Homogeneous built-in `asset.propose` Candidates entered the full projection-context loader, materializing unrelated closed history before pure effect projection. This stage is distinct from Asset consequences before and inside the writer.

## Resulting behavior

A generic projection-context factory owns read dispatch; the commitment coordinator only calls that factory. An optional semantic Store reader selects the exact claim-bound parent and, for a single effect, every matching Candidate receipt/head and every referenced member at one read snapshot. An unbound multi-effect batch needs no historical context. The existing exact-replay owner still validates complete proof; selected IDs do not authorize replay or output. SQLite owns indexed lookups and bind-limit chunking. Selection is read-only and preserves caller transaction ownership.

Only exact homogeneous built-in shapes use the hint. Mixed, graph, injected, uncertain shapes and Stores without the optional reader retain full context. Authentication, pure projection, protected outputs, claim/keys and writer-time consequence rechecks retain their owners. Known hint-shape uncertainty alone falls back; storage and unknown failures propagate. No table, fact, scheduler, cache authority or required Store capability is added.

## Verification and limits

The corrected five-file component passed independent conformance, 216 affected/governance tests and full-context semantic/selector probes; exact scope and retained failures are in the [plan](../docs/plans/asset-proposal-projection-context.md). Root combination, artifact and overall gates are tracked in the [development report](../reports/113-asset-context-direct-transport-development-2026-10-25.md). This localizes only effect-projection context: pre-commit/writer FactReducer, claim and other historical consumers remain open.
