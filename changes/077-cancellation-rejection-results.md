# Cancellation rejection results

When cancellation publishes a signed Candidate that is rejected and its exact
durable rejection fact verifies, the shared application owner returns the
rejection result and observed SQLite revision through both single and batch
commands. Single JSON failures retain `cancel_rejected` and add
`candidate_accepted: false`, `facts_retained: true`, exact `data`, and `revision`.
Bulk rows include the same exact rejection IDs and per-operation revision.
Human rejection output includes the durable rejection record reference. A
post-commit pending response preserves the operation revision even if a later
write advances the Store before the CLI renders it. Redis preflight failure has
no operation result and does not claim that facts were retained.

The CLI classifies only the typed, verified cancellation rejection as an
expected refusal. Missing or invalid proof and other programming errors remain
safe internal/unknown outcomes; a batch stops and marks later targets not
attempted. A missing Contract remains `not_found`. Authentication rejection
continues to expose only untrusted claimed-ID metadata, never authenticated
Candidate or claim identity.

Signing, current authority, causal parent, idempotency keys, replay, and required
Redis preflight/relay behavior are unchanged. Accepted or rejected post-commit
relay-pending responses keep the existing decision and exact result. There is no
new transaction, fact type, or cancellation policy.

Validation includes ordinary signed fixtures and an installed control using
native local Redis with zero Worker or provider calls. The installed harness
executed 15 command phases but exited nonzero because it required optional
acceptance flags on an accepted bulk row that the CLI does not emit. A later
read-only recovery verified the retained single rejection, bulk rejection and
accepted result, and reported a neutral accepted replay with an 18-table,
schema and PRAGMA fingerprint. The outer recovery wrapper retained a failure
because a previously absent WAL path appeared as an empty zero-frame file; the
main database hash remained unchanged. These are recovered installed results,
not an all-passing installed run.
