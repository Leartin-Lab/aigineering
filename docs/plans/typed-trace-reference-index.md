# E1: Candidate-head index for Redis trace capture

**Status:** The bounded component passed independent review. The exact Root development combination passed independent overall review with an open reliability incident; final metadata closure and whole-stage qualification are separate. See the [stage
plan](sustained-engineering-delivery.md) and [required Redis boundary](../adr/ADR-027-required-redis-collaboration.md).

## Goal and boundary

E1 removes repeated whole-map Candidate-head selection from typed commitment-trace attribution during one Redis event capture. It builds one
ephemeral reverse index from the effective `records_by_id` mapping and retains the existing typed recursive Contract-reference resolver as
the sole attribution policy.

Only `core/session.py` and `redis_collaboration.py` own production changes; only `tests/test_runtime_records.py` and
`tests/test_redis_collaboration.py` own test changes. `_capture_events()` is the only migrated caller. Session export, CLI cycle,
local-Fleet cycle and audit callers retain their legacy Mapping path. No Store port, SQL schema, Redis protocol, authority rule, fact,
claim, prefix proof, Worker-causality rule or persistent cache changes.

The context is built from `_records()`' already coalesced by-ID mapping, not raw rows. It retains that exact capture-local mapping; the
capture owner does not mutate it during use. The index buckets are read-only tuples with global value positions, and no context crosses a
capture or source refresh.

## Resolver compatibility

The builder indexes every `candidate.committed` record whose `candidate_id` has exact type `str`. Several surviving record IDs may share one
Candidate ID; all heads remain indexed. Duplicate raw record IDs already overwritten by `_records()` are not resurrected.

For a commitment trace, the fast path retrieves all heads for its unique parsed Candidate IDs, sorts them by their original global
mapping-value position, and then follows the existing exact `committed_record_ids` through the same mapping. Recursive typed reads, shared
`visited`, member order, parse/filter behavior and error propagation remain in the existing owner. Empty ordinary mappings and `None` retain
their former truthiness behavior; a context is recognized before truthiness is applied.

Any committed head with a non-exact-string Candidate ID marks the context for legacy fallback. Building the context does not hash, compare,
validate or raise for that value. Only a commitment-trace branch performs the original full ordered scan, preserving lazy errors such as an
unhashable `mappingproxy`, including when no Candidate IDs were extracted. Direct traces do not enter that branch. This retains
malformed-history behavior; it does not repair or qualify malformed records.

## Binding and acceptance

Ready plan v2 SHA-256: `1561e44efc78b64c007951a974ac84b2b42dc77dd89d4b94b3940ac5d624454c`. Independent Ready review SHA-256:
`185d89b2b8a9ee41eaa2406eacf693569db93ddb4c4b7b96af670f8df2727e12`. The earlier v1 plan was NotReady; v2 closed the lazy malformed-ID and
global-order blockers before implementation.

The independent component review passed against the exact isolated 1,043-file source map
`234172e78829ecd593b9316ff9f0aa85542e70e6cdd818862d2b530a5c0b16c3` and four-file owner map
`cc9cf010edaceaf03d29a40c264f8ef7da8f22d452b7a7aad291436142a1bdf4`. Independent review SHA-256:
`20fb71d2715ef87205f68c76dd0e0fab0120682b471a9ee3ad031b9ea7c05e0c`.

The review independently reconstructed and checked the exact source files, reran the five declared test modules, and reproduced both signed
capture families. It recorded 49 tests passed; eight native Redis integration cases were skipped/NotRun because `AIG_REDIS_TEST_URL` was
absent. No real Redis client, sync or network call was used. A real Redis integration check is still required in a later dedicated
integration.

## Signed capture evidence

The E0 builder creates normal signed history and completes sequential historical work through claim and WorkerHost execution. Each fixed
snapshot was captured twice; exact legacy event fields/digests and source revision matched. Counts are actual Python RuntimeRecord/Mapping
operations inside `_capture_events()`, not SQL rows, VM instructions or end-to-end sync time.

| Fixture family | H | N | T | U | R | candidateViews | K | Get total | Worker closure | Legacy T×N | New values | Fallback scans |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Sequential | 0 | 16 | 3 | 3 | 3 | 3 | 12 | 39 | 27 | 48 | 16 | 0 |
| Sequential | 16 | 416 | 19 | 19 | 19 | 19 | 204 | 1,383 | 1,179 | 7,904 | 416 | 0 |
| Sequential | 64 | 1,616 | 67 | 67 | 67 | 67 | 780 | 5,415 | 4,635 | 108,272 | 1,616 | 0 |
| Batch declarations | 0 | 16 | 3 | 3 | 3 | 3 | 12 | 39 | 27 | 48 | 16 | 0 |
| Batch declarations | 16 | 371 | 4 | 4 | 4 | 4 | 204 | 1,368 | 1,164 | 1,484 | 371 | 0 |
| Batch declarations | 64 | 1,427 | 4 | 4 | 4 | 4 | 780 | 5,352 | 4,572 | 5,708 | 1,427 | 0 |

Every normal signed sample used the fast path; fallback scans were zero. In these samples, `Get total` is K plus Worker-closure gets; other exact-ID gets were also observed and were zero. The index reduces only
repeated Candidate-head selection. Complete-prefix enumeration/materialization still visits N records; building the index is O(N), selected global ordering may cost O(R
log R), and recursive member/Worker closure work remains. Full-prefix proof, deep readiness and other E2 work remain unmeasured. The four
excluded callers are not optimized. No total-sync or H-independent claim follows.

The author retained an expected API-absent test-collection red and early probe-harness setup failures separately; the independent reviewer
reproduced the final commands and probe successfully. Those setup failures are not product failures or passing evidence.

## Remaining integration

This component result does not accept the current Root combination or the overall engineering stage. Root rebound the exact combination and completed full/installed/build and dedicated Redis checks. Its separate non-author overall review accepted this bounded development increment with the original intermittent Fleet incident still open. The E1 review is not that overall review. Existing required Redis, complete-prefix and writer/fact-authority boundaries remain in
force.

Later Root combination evidence is recorded separately in [report 115](../../reports/115-native-capture-development-2026-10-26.md). Root source tests actually executed the eight native Redis cases; the component review above remains 49 passed plus eight NotRun. The installed combination retains an unattributed intermittent Fleet failure alongside a passing full observed rerun. Separate non-author overall acceptance passed for the exact bounded increment, retaining the original unexplained partial workflow failure.
