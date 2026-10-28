# Change 101: Fence concurrent Redis query generations and reads

Redis query rebuilds now watch the active pointer and both relevant generation
metadata records through `EXEC`, refusing a captured snapshot older than the
active or target revision before queuing entity writes. Incremental catch-up
checks the exact expected generation revision while watching metadata, and
commits entity changes with the new watermark. Conflicts retry within a bound.

Cached multi-command reads retain a metadata watch through a validating no-op
`EXEC` and recheck the SQLite source revision after `EXEC` succeeds and before
returning. A SQLite-only commit in that window discards the result and uses the
bounded read retry. Compatibility fallbacks and revision-keyed JSON view builds also check that source revision
remains stable. Unchanged reads do not capture or hash the whole Store. These
checks cover concurrent writers over the same history; source identity across
same-revision Stores and explicit restoration of polluted generations remain
separate work.

Verification uses signed SQLite histories with two connections and native Redis
interleavings at the real `EXEC` boundary, including a signed SQLite-only
commit immediately before a validating read `EXEC`. It verifies stale rebuild
refusal, metadata-only invalidation, competing catch-up, read-result discard/retry,
fallback and JSON source checks, and bounded conflict exhaustion. This is local
fixture evidence, not provider execution or full Redis/source-identity
qualification.
