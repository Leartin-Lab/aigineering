# v0.5.1 Redis query projection acceptance

Baseline: v0.5.0 commit `0061cf5`
Implementation commits: `3065c33`, `d7736f1`
Scope: optional Redis read projection over authoritative SQLite facts
Status: passed

## Boundary result

- SQLite remains the only fact, Candidate, claim, allowance, acceptance,
  idempotency, and terminal authority.
- Redis code is isolated under the adapter layer. Architecture tests forbid
  Redis dependencies in correctness owners.
- Every cache namespace binds Genesis domain and projection schema.
- Complete generations activate atomically.
- RuntimeRecord catch-up advances a monotonic authoritative revision in the
  same Redis transaction as derived entity updates.
- Current reads detect missing, invalid, or stale generations before use.
- Redis connection failure produces a visible degradation message and returns
  the authoritative SQLite view.

## Real Redis verification

Provider: redis-py 8.1.0
Server: `redis:alpine`, bound only to localhost for the test

Verified:

- empty Redis full rebuild;
- full flush followed by automatic rebuild;
- incremental Asset and Contract catch-up;
- revision-bound task-view invalidation;
- incomplete generation rejection;
- missing indexed payload repair;
- Redis stop followed by SQLite fallback;
- empty Redis restart followed by reconstruction;
- two SQLite connections sharing one Redis generation;
- concurrent same-domain rebuild convergence;
- separate domains using disjoint namespaces;
- CLI and FastAPI read behavior with Redis enabled.

The stopped-Redis CLI returned the same Asset list and task-status projection
as the preceding Redis-backed read. No task, claim, or fact transition depended
on cache availability.

## Deterministic gate

```text
1093 passed with Redis enabled
ruff check: passed
ruff format --check: passed
wheel and sdist build: passed
Twine metadata check: passed
installed-wheel CLI and Redis rebuild smoke: passed
```

The one emitted warning is an upstream FastAPI/Starlette test-client
deprecation and does not affect runtime behavior.

## Limits

- Redis is optional and does not provide distributed consensus or scheduling.
- v0.5.1 caches exact read models; semantic or embedding search is not included.
- Global RuntimeRecord revision invalidates task-view memoization
  conservatively, even when an unrelated task changed.
- Redis deployment authentication, TLS, persistence, and resource policy remain
  operator responsibilities.
