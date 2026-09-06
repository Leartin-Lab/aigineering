# Change 015: Tool validation patches

Status: Implemented and verified
Target: v0.5.7
Decision: ADR-020

Non-object tool arguments previously became an empty object and could invoke
an unconstrained handler. ToolWorker now returns a typed `ToolActionError`
observation without invocation. The same signed observation path retains the
error metadata.

Schema `const` and `enum` used Python equality, which conflates booleans and
numbers. JSON structural comparison now preserves that distinction recursively
while retaining numeric equality and frozen-array compatibility. Explicit null
schema keywords fail registration, except the valid `const: null` case.

Historical ADR implementation notes distinguish the retained subtask and source
isolation principles from superseded Method-owned implementation. No protocol
identity, Store schema, tool side-effect semantics, or commitment owner changes.

Verification is recorded in `reports/057-tool-validation-patches-2026-09-06.md`.
