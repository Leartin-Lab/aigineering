# Signed task holds and claim arbitration

Date: 2026-10-04
Source version: 0.5.13.dev0; development only, no release tag

The task CLI adds signed `block`, exact `unblock`, and hold-history inspection.
The collaboration plugin publishes generic claim-hold facts; SQLite arbitrates
placement against claim acquisition and competing releases inside its existing
writer transactions. Runtime views derive blockers from the same ordered facts.

Every hold has its own placement record ID, reason and authenticated actor/key.
Any open hold prevents claim acquisition. Releasing one leaves other holds open.
Exact keyed replay returns the original durable record after later state changes.
Terminal tasks retain their original outcome, while old holds may be cleaned up.

Placement refuses already claimed or terminal work. This slice does not pause an
active Worker, reopen an old attempt, implement review requests or create a
handoff successor. Those remain part of the continuing CLI goal. Redis remains
required; post-commit synchronization failures retain accepted SQLite facts.

Collaboration commands provide readable text by default, including complete
comment, hold and release IDs. Optional `--json` retains the existing envelope.
Command registration copies Click command objects so main-entry protection
cannot contaminate isolated registrations. A transaction-race rejection is
relayed after its SQLite commit; a failed relay reports synchronization pending.

Decision: [ADR-029](../docs/adr/ADR-029-signed-claim-holds.md).
Usage: [task operations](../docs/reference/task-board.md).
Evidence: [development verification](../reports/075-signed-task-holds-2026-10-04.md).
