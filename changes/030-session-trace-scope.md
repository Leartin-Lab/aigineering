# 030: Root-scoped session evidence

Status: implemented

Quick-run session manifests now describe one root Contract and its descendant
tree. They preserve frozen context bindings and exact Asset IDs referenced by
tree traces as evidence, and separately list Assets committed during the
execution window as produced outputs. Contract input names alone do not cause
all same-name Assets to be included. Sessions record the runtime revision
window and trace IDs exported from that bounded interval. Trace ownership
follows typed Contract references and committed record links; events whose root
cannot be proven are listed as unassigned instead of being attributed by a
sentinel label.

Target-pool cycle summaries use the same typed-reference helper over all
RuntimeRecords in each bounded revision window. Commitment sentinel traces
are related through candidate IDs and committed-record links; the sentinel
string is never emitted as a Contract ID. Unproven trace IDs appear in
`unassigned_trace_ids`, and a cycle with trace events is retained even when no
Contract reference can be established.

Runtime-record scans accept an optional inclusive upper revision bound while
retaining their existing unbounded default. SQLite Store snapshots use the
SQLite backup API; path snapshots open the source in read-only mode and reject
schema mismatches without upgrading it. Existing session JSON and runtime
records are not rewritten, while newly written trace JSON in SQLite and JSONL
uses stable key ordering. The SQLite schema version is unchanged. Old session
manifests are surfaced as `legacy_unscoped` because their former all-store
scope cannot be reconstructed safely.
