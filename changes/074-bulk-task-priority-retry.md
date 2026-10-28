# Bulk task priority and retry

`aig task priority TASK... VALUE` and `aig task retry TASK...` now process one
through 64 distinct Contract IDs in input order through the existing signed
application owners. Each target commits independently. Priority batches require
a complete exact `--previous-for TASK=HEAD` map; `TASK=` means that no priority
fact existed. Retry reuses each original task's deterministic successor and
Candidate key. Its batch correlation ID is reporting metadata, never a
Candidate idempotency key. The one-target forms retain their previous syntax
and results.

Both commands return every per-target outcome. Known not-found and typed
business rejection outcomes allow later targets to run. Accepted/pending and
durable-rejection results retain the exact operation IDs, actual key and
observed revision. A priority-state rejection found during read validation has
no Candidate or rejection fact; its row reports `candidate_accepted: null`,
`command_attempted: false` and the observed snapshot revision. A prepublication
failure invents no result; an unexpected
failure stops later calls and labels remaining targets `not_attempted`.
Required Redis preflight still precedes publication, and replay uses the same
canonical owner. Priority remains advisory: it changes ordering only, never
eligibility, claims or successor priority.

Examples:

```bash
aig task priority task:v3:... task:v3:... 80 \
  --previous-for task:v3:...= --previous-for task:v3:...= \
  --reason "Raise these tasks" --idempotency-key priority-batch --json
aig task retry task:v6:... task:v6:... --json
```

Validation used isolated signed SQLite fixtures and fixture-backed relay
failures. It did not invoke a provider or establish native Redis or installed
artifact behavior.
