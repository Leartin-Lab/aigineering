# Task context view

`aig task context CONTRACT_ID` now shows the current Worker disclosure selection
and method continuation context as separate metadata-only lists. The view shares
the existing disclosure selector and method-context reader with Worker package
construction, so it reports current selection without changing claim or execution
behavior.

An exact `--asset ASSET_ID` may select one listed Asset. Its content is included
only with `--include-content`; the existing redacted representation stays
redacted, while nonpromptable and tombstoned content is refused. Metadata and
errors omit content, policy explanations, and private source details. Queries
retain the required Redis collaboration check and read from an immutable SQLite
snapshot.

See [bounded integrated verification](../reports/092-task-context-view-2026-10-07.md).
