# Preserve structured planning compile failure diagnostics

Status: Implemented development increment
Date: 2026-10-23

Planning compilation previously reduced rejection objects to a message and field
names. The existing generic recovery received that summary but lost the child,
expected/actual values and recoverability information needed to inspect the
rejection. Both plan and replan compilation now carry immutable rejection values
through the Worker error boundary. The existing failure transaction may include
an optional `compile_diagnostics` object, validated by one pure application owner.

Version 1 preserves source order and optional fields. It retains at most 16
entries, with at most 512 bytes per JSON string literal and 8,192 bytes for the
whole object, measured using compact, sorted, ASCII JSON. Each entry contains
`values` and a sorted `truncated_fields` list. The object records `version`,
`original_count`, `omitted_count` and `entries`. Short diagnostics retain their
values; long diagnostics explicitly disclose truncation and omitted entries.
Only the defined text, boolean and budget-count fields are accepted. Invalid
adapter details retain the existing textual failure path rather than fabricate
structured evidence. Early compile errors without rejection objects also retain
that path.

The recovery processor keeps the existing category, name and readable rejection
summary. It nests the validated details in that summary before calling the same
generic recovery publisher. Frozen RuntimeRecord values are recursively thawed
into fresh JSON containers. Claim diagnostics retain their strict failure-field
allowlist and validate the optional format and compile category; malformed new
fields remain integrity failures. They do not expose this additional text in
ordinary task-run or failure-binding summaries.

The original failed terminal and release remain immutable. This change does not
alter recovery routing, retries, allowance, Contract scope, accepted effects or
independent qualification. A `recoverable` value is diagnostic information, not
permission to retry. Tests cover authenticated claims and durable failure/release,
real accepted recovery publication, disclosure in a newly claimed package, SQLite
reopen, exact failure
proof and lineage, bounded diagnostics, and interruption after publication before
the outcome record. Publication or disclosure does not establish successful
blueprint correction or business completion.

Deploy the writer, reader and consumer together after stopping older Workers;
there is no schema migration or new reader-floor gate. Old failure records lack
the optional field and reconstruct their unchanged legacy summary. New records
deterministically select version 1, including after a publication/outcome
interruption. Do not deploy an intermediate writer with the old consumer: that
could publish a different recovery intent for the same failure. A code-only
rollback does not remove durable version-1 records or published Assets. Retain
the compatible diagnostic reader for those records, and retain the consumer
until all pending version-1 recovery outcomes have been reconciled. Exact
publication proof is never weakened to accommodate a mismatching intent.
