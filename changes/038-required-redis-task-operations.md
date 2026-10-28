# Required Redis collaboration and usable task operations

Date: 2026-10-03
Source version: 0.5.13.dev0; development only, no release tag

The CLI now discovers and operates tasks through `task list/board/stats/show`,
`result/cancel/retry`, and `events/watch`. Views use shared application task
projection. Boards group fact-derived statuses; they do not own mutable task
state. List reports filtering and truncation, and task scopes use exact Contract
IDs and immutable ancestry.

Redis is a base dependency and required application component for read indexes,
revision-addressed event feeds and bounded Worker wakeups. Normal CLI leaf
commands and HTTP Store requests preflight it; strict application reads do not
silently fall back. Help and explicitly selected offline audit views remain
available during incidents. Local Compose configuration includes persistent Redis
storage. `redis status/sync/rebuild` owns maintenance; legacy `cache` delegates.

Committed SQLite RuntimeRecords supply an outbox that can be replayed after
process interruption. Lua-atomic watermark/event updates and conditional state
repair support concurrent relay. Typed stream metadata excludes user content and
secrets. Redis failure after commitment retains SQLite facts and reports sync
pending; it does not fabricate a Candidate rejection or reset an existing claim.

Task creation and legacy Contract publication share one validation/publication
owner. Retry commands share one deterministic signed publisher and frozen context.
Task errors, timeout and unsuccessful terminals return nonzero. Connections close
on success and failure. Result export checks integrity, exact independent
qualification and exportable business Assets, preserves exact UTF-8 bytes, and
never overwrites a destination. Fleet target execution no longer claims unrelated
workspace roots.

These changes preserve the existing signed wire protocol and SQLite transaction
authority. Arbitrary editable card states, human comments, manual block/unblock,
multi-board administration and a separate dispatcher are not added. Historical
release/provider evidence is not requalified by this development slice.

Decision: [ADR-027](../docs/adr/ADR-027-required-redis-collaboration.md).
Usage: [task operations](../docs/reference/task-board.md).
