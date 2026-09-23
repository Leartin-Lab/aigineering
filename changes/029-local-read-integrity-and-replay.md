# 029: Local read integrity and replay evidence

Status: implemented

Task status and audit read a consistent read-only SQLite snapshot. A terminal
result from `task wait` is checked once against its snapshot; a timed-out wait
explicitly reports that terminal integrity was not checked. The
integrity result checks runtime-record content addresses and compares the
current materialized runtime views with a rebuild on a separate in-memory
snapshot. An inconsistent result leaves the projected business status visible,
sets `ok` to false, and makes the CLI return a non-zero status. Audit findings
contain error categories and identifiers rather than record payloads.

Replay now compares session manifest trace IDs with the complete selected JSONL
trace, checks those trace payloads against immutable SQLite trace records, and
reports candidate decision, effect, and accepted output counts. Legacy fragment
counts are explicitly limited to projection and tool-execution trace events.
SQLite record revisions and `recorded_at` values are reported as provenance;
the trace event's own timestamp remains unchanged.

Failure projections expose terminal descendant blockers and structured failure
details. Tool errors from related task branches retain their source Contract and
trace IDs, and remain labeled as related evidence unless the failed Contract
itself carries that error event. Observation references expose Asset IDs only.

These checks establish local consistency between the available runtime records
and their materialized views. They do not establish runtime-log completeness or
protection from a coordinated database rewrite; those require an external
trusted checkpoint. Actor-signature authenticity is not checked here and would
require re-verifying signatures against trusted Genesis and authorized keys.
