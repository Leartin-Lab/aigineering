# Fleet stop metadata is monotonic within its owner process

The persistent Fleet child now serializes state metadata read/merge/write operations
and prevents a late `ready` update from reopening a generation after stop has been
observed. `stopped`, `failed`, and the existing `degraded` snapshots are frozen,
including their `active_slots` and `error_code` observations. The child publishes
successful `stopped` only after the slot runner and stop watcher have exited and its
maintenance Store has closed.

A stop timeout continues to return `stopping` as an unacknowledged request result;
metadata may remain `ready` until the child observes the request. The process-local
metadata lock does not arbitrate task facts, claims, or arbitrary writes from another
process. A bounded predicate poll in the stop/drain test waits for observed `stopping`
while the admitted Worker remains blocked, then verifies one claim after release and
final drain. Separate deterministic controls cover serialized ready/stopping writes,
late ready updates, and late watcher writes against terminal snapshots.

Focused verification passed: with `AIG_REDIS_TEST_URL` and
`AIGINEERING_REDIS_URL` set to a dedicated Redis test service,
`pytest -q tests/test_fleet_service.py` reported 27 passed.
`ruff check src/aigineering/fleet_service.py tests/test_fleet_service.py` and
`ruff format --check src/aigineering/fleet_service.py tests/test_fleet_service.py`
also passed.
