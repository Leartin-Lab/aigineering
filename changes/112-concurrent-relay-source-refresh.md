# Refresh a source snapshot after a concurrent Redis relay

Status: Implemented development increment
Date: 2026-10-23

`sync()` captures one immutable source prefix before checking Redis. If another
service commits and relays a later prefix while that capture is in flight, the
first service can observe Redis ahead of its stale snapshot and report a false
conflict. For this `redis_ahead` result only, sync now captures again from its
same configured Store and retries the full prefix proof and append attempt when
the revision advanced and the new capture preserves every entry in the original
prefix.

If the Store did not advance, sync retains the original `redis_ahead` result and
leaves Redis unchanged. If the refreshed capture changed the original prefix,
sync reports `source_diverged` without writing Redis. The bounded attempt budget
also covers refreshes; the final failed attempt does not recapture and reports
`sync_contention`. Watch conflicts keep their existing retry behavior. Other
errors, rebuild snapshot factories, Redis projection validation and the SQLite
authority boundary are unchanged.

The `events()` reader keeps its one frozen source capture and its existing
bounded prefix-proof loop. If its own sync attempt exhausts its retry budget,
the reader may start another iteration of that same proof loop for
`sync_contention`; it does not recapture the source or return events until Redis
again proves the captured prefix. Other sync errors remain visible, and the
reader's own final exhausted attempt still reports contention.

Tests cover two services on independent SQLite connections, a signed Candidate
commit followed by a concurrent relay, forked fake-source prefix divergence,
genuine Redis-ahead history, and retry-budget exhaustion. The Redis integration
test uses a unique domain namespace and isolated Redis test database. It does
not use an external snapshot factory or rewrite source history.
