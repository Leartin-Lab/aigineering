# Change 003: Redis query projection

Status: In progress
Target: 0.5.1
Public decision: `docs/adr/ADR-016-disposable-redis-query-projection.md`
Depends on: v0.5.0 Candidate commitment and runtime reconstruction

## Problem

The v0.5.0 reference runtime correctly reconstructs state from SQLite, but
several read paths repeatedly scan or recompute the same Asset, Contract,
runtime-record, and task-view relationships. That is acceptable for the local
reference scale and wasteful for longer histories and concurrent readers.

Adding a cache naively would create a second source of truth, a dual-write
transaction, or a hidden task-state owner. Each would break restart and
active-active guarantees.

## Intended change

Add an optional Redis-backed query projection with these boundaries:

- SQLite and immutable RuntimeRecords remain authoritative.
- Candidate commitment, claims, fencing, allowance, authority, idempotency, and
  terminal uniqueness never depend on Redis.
- Redis contains only versioned derived keys and has no mutation API exposed to
  Workers or Plugins.
- Projection updates are replayable from an authoritative record watermark.
- A missing, stale, flushed, or unavailable Redis instance triggers rebuild or
  authoritative fallback, never a different protocol decision.
- Cache namespaces bind the Store domain and projection schema version.
- The first slice accelerates read-only entity, relationship, and task-view
  queries. Correctness-sensitive Store transactions continue to query SQLite.

## Ordered implementation

1. Define a small `QueryProjection` interface and one semantic snapshot format.
2. Add deterministic SQLite export and semantic-digest tests.
3. Add the optional Redis adapter and configuration without making Redis a base
   dependency.
4. Rebuild an empty Redis instance from SQLite and verify equal results.
5. Add incremental catch-up from RuntimeRecords and a versioned watermark.
6. Route CLI/server read views through the projection while preserving
   authoritative fallback.
7. Exercise Redis loss, stale generation, partial refresh, restart, and two
   reader processes.
8. Update public use instructions and release evidence only after the adapter
   passes all gates.

## Non-goals

- distributed locks or leases in Redis;
- Redis as a Candidate, claim, task, or terminal store;
- cross-machine consensus;
- an embedding or semantic-equivalence index;
- removal of SQLite query support required for rebuild and fallback.

## Exit criteria

- deleting all Redis keys and rebuilding yields the same semantic query digest;
- Redis unavailability changes latency or availability of optional read views,
  not commitment results;
- stale projection data is detected before it is used as a current view;
- no Plugin, Worker, projector, or authority checker imports Redis;
- MemoryStore and SQLite behavior remain conformant;
- focused, architecture, restart, concurrency, full-suite, build, and
  installed-artifact checks pass.
