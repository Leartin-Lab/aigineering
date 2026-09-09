# 019: Runtime boundary cleanup

Status: implemented
Version: v0.5.10
Date: 2026-09-08

This follow-up removes glue and broad capabilities without changing the wire,
SQLite schema, or commitment transaction.

- Narrow read and write capability Protocols replace broad Store access in
  projections, verification, disclosure, productivity, and completion plugins.
- Completion plugins are statically prevented from importing commitment Store
  capabilities.
- Canonical derived terminal and trace commitment belongs to the core lifecycle
  owner; completion Plugins request the outcome without calling the Store commit
  primitive.
- Pure SQLite row materialization moved behind an internal module; SQLiteStore
  retains the public API and remains the sole connection and transaction owner.
- TraceEntry-to-RuntimeRecord encoding uses one canonical helper, preserving
  causal parents and explicit record timestamps.
- HTTP Contract responses expose their complete authority, routing, disclosure,
  context, and acceptance fields instead of an ambiguous partial entity.
- Canonical identity and signing primitives now belong to the protocol layer;
  the former core modules are compatibility re-exports with identical public
  objects, and protocol code has no reverse dependency on core.
- A named legacy JSONL adapter is available to new compatibility consumers,
  while core replay retains the old owner to avoid a core-to-adapter dependency
  cycle during the public deprecation window.

Physical JSONL ownership removal, incremental consequence
watermarks, and deeper SQLite ledger decomposition remain separate future work;
none is represented as completed by this change.
