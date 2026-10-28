# Assignment and priority pending results

`aig task assign`, `aig task reassign`, and `aig task priority` now preserve the
exact signed operation result when SQLite commits but required Redis relay
synchronization remains pending. The nonzero `redis_sync_pending` response
includes the committed fact, Candidate ID, actual idempotency key, fact IDs, and
observed SQLite revision. This makes the same intent replayable after Redis
recovers, including when the original command generated its key.

The result is built from the operation's exact commitment decision and verified
durable member, before relay synchronization. A retained rejection reports only
its exact rejection fact IDs and is not represented as an accepted assignment
or priority. A pre-publication Redis failure returns no operation result.
Unexpected errors use a fixed `internal_error` response and remain chained
internally; they are not relabeled as Redis outage or business rejection.
Unexpected snapshot/store construction errors for these mutation commands use
the same safe response. A missing workspace keeps the existing
`workspace_not_initialized` result, and Click exit control flow remains intact.
Human pending hints print generated keys and explicit keys made from a safe
ASCII single-token character set. Other valid keys are not echoed to the
terminal; the exact value remains available in the structured JSON result.

The existing assignment and priority replay fences remain in force. Replaying
the returned key yields the same Candidate and operation fact without adding a
duplicate history entry. Reassignment and priority updates continue to bind
their exact previous head. The focused tests use isolated SQLite stores and
injected relay outcomes; they do not claim installed Redis outage verification.
