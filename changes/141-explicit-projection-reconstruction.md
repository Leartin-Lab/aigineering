# Explicit projection reconstruction

Opening a current-schema SQLite Store returns from schema initialization after
compatible table/index setup, before migration and graph reconstruction. Ordinary signed
publication already materializes graph facts transactionally. Current reads
therefore reuse the existing projection rather than deleting and repopulating
it from all runtime history.

Compatible indexes may be installed on supported existing Stores; this one-time
work is separate from replaying facts. New Store initialization, older-schema
upgrades and explicit reconstruction keep
their existing paths. Missing projection rows are no longer silently repaired
by ordinary reopen; reconstruction remains an explicit operation. Schema 24,
Asset identity, exact replay, task satisfaction and authority are unchanged.
This closes the startup debt observed in change 140; it does not claim that all
CLI operations have history-independent cost.

The shared Worker compiler also calls its existing graph-output owner directly
for ordinary and attested outputs; a forwarding-only wrapper was removed.
There is no additional compiler, cache, controller or durable table.

Checks cover signed populated Store reopen with history/migration/rebuild calls
forbidden, unchanged graph materialization, legacy upgrade, explicit recovery of
missing projection rows, strict reconstruction and canonical Worker outputs.
