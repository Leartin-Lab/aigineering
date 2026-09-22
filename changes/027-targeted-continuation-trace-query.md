# Targeted continuation trace reads

Continuation worker packaging now reads trace events by `relation_target` before
filtering the two method-context event types. The returned order, de-duplication,
missing-asset handling, and disclosure redaction remain unchanged.

SQLite creates the `relation_target` index idempotently whenever a store opens,
including an existing database. This is an ordinary index initialization change;
there is no schema version bump or new fact. Custom adapters implementing
`OperationalStoreProtocol` must provide `get_by_relation_target`.

Validation covers Memory, JSONL, and SQLite adapter conformance, reopening an
existing SQLite database, SQLite query-plan use of the index, and the public
continuation context path without a full-trace read.
