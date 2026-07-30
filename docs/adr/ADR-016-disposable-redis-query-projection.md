# ADR-016: Disposable Redis query projection

Status: Accepted
Date: 2026-07-31
Related: ADR-003, ADR-010, ADR-011, ADR-013

## Context

SQLite is the v0.5 source of truth and transaction arbiter. Runtime records can
reconstruct all materialized state. Read-heavy task, Asset, lineage, and audit
views nevertheless repeat database queries and deterministic reductions.

Redis can reduce those reads only if it remains a disposable projection.
Treating it as a queue, lock owner, claim store, or second write authority
would make correctness depend on a non-atomic dual write.

## Decision

Aigineering will support an optional Redis query projection.

The projection:

- is derived exclusively from authoritative SQLite facts and RuntimeRecords;
- uses a Store-domain namespace, projection schema version, and authoritative
  watermark;
- exposes read models, never fact mutation;
- detects an absent or stale generation before returning a current view;
- can be flushed and deterministically rebuilt;
- may fall back to the authoritative SQLite projection when unavailable.

Candidate commitment, authority, allowance, acceptance, claims, fencing,
idempotency, and terminal decisions must not read Redis.

The Python reference implementation keeps Redis in an optional dependency
extra. A language implementing the protocol does not need Redis.

## Consequences

- read performance can scale independently of the commitment boundary;
- a Redis outage cannot corrupt facts, although it may reduce read performance;
- incremental projection needs explicit watermarks and failure tests;
- SQLite query paths required for rebuild and fallback remain supported;
- this decision does not provide distributed execution or consensus.

## Evidence

- `tests/test_redis_query_projection.py`
- `tests/test_cli_cache.py`
- `tests/architecture/test_governance.py`
- `reports/051-redis-query-projection-2026-07-31.md`
