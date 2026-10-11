# Verified retry-publication lineage

`aig task lineage` now includes retry edges proved from accepted signed
publications. The target declaration must have an exact receipt, committed head
and member proof, name its source through the receipt's sole causal parent, and
match the complete successor produced by the canonical retry semantics. The
source also needs an exact accepted declaration proof; that declaration may be
part of a compound signed publication. Lineage uses the shared generic
publication-history proof owner and the same immutable task-view query used by
the existing lineage relations.

The read-only component is bidirectional. Both endpoints retain their normal
task status and blockers. Missing, duplicate or malformed target evidence
produces a safe issue without expanding the component; when a valid target
receipt identifies the source, that issue is visible from the source endpoint.
The receipt does not contain the retry result's idempotency key, and lineage
does not infer one-to-one failure attribution from diagnostics. Other Stage 2
families, including handoff, revision, tool request/observation/continuation and
other recovery causes, remain outside the verified view.

See [the task-board reference](../docs/reference/task-board.md) for the CLI
behavior and remaining relation coverage.
