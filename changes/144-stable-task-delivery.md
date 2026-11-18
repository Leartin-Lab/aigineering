# Stable task delivery and diagnostics

Status: Implemented

Completed task status, quick GOAL and formal task export use the canonical Asset
selection for the task's declared outputs. A single-output export checks
disclosure for the selected slot. Engine delivery checks disclosure for each
required output and records the exact cross-domain mapping. A retry verifies
the recorded publication and reuses its original Asset IDs. Name-based CLI
slicing and the latest marker in `asset versions` use each Asset's first-commit
revision when publication order is available.

Readiness checks frozen input bindings before reporting missing inputs, version
conflicts or insufficient trust. A compatibility recovery with both
`--recreate` and `--cancel` commits the replacement and source cancellation in
one Candidate per source. For an older partial action, the command verifies the
prior replacement before finishing cancellation. If an existing replacement
has no exact proof for this operation, the command reports the source and
replacement IDs.

Task productivity reports a token total only if every observed model usage
record supplies that field. The corresponding coverage reports the known
subtotal and observation counts. A provider's explicit zero remains a reported
value. Causal allowance remains a separate measure. Malformed native tool
calls preserve any usage already reported by the provider.

Quick GOAL can deliver its formal result even when optional session capture
fails. A task wait reads from one consistent SQLite snapshot per poll. Terminal
wait results still receive an isolated integrity check. Targeted Fleet reads
follow indexed parent-child relationships. Redis event waits retry transient
contention within their original deadline and verify the complete prefix in
bounded pages.
