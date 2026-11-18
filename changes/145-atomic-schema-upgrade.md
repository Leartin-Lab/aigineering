# Atomic SQLite schema upgrades

Status: Implemented

SQLite upgrades apply the schema version and rebuild the Asset graph in one
writer transaction. A reconstruction failure rolls back both so the upgrade
can be retried from the previous version. The explicit `aig domain upgrade`
command retains its pre-upgrade backup.

Concurrent opening processes recheck the schema version after acquiring the
writer lock, so only the first successful upgrader runs migrations and graph
reconstruction. Ordinary current-schema opening keeps its compatible index
setup and reads the existing graph. Explicit graph reconstruction still has
its own transaction.

Tests cover a retained backup and unchanged schema, Asset graph and runtime
facts after an injected reconstruction failure. The same Store upgrades on
retry. A two-connection test verifies that a waiter sees the committed version
and skips a duplicate migration.
