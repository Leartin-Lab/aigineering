# Task-view aggregate query reuse

Aggregate `task list`, `task board` and `task stats` now build one immutable
revision-bearing snapshot for each command. The snapshot supplies the canonical
runtime projection and existing lifecycle validators with the same complete
runtime history, Contracts, Assets, trace entries and registrations. The shared
read facade exposes only the methods these projections need; operational-store
validation still runs against the original Store.

The query context binds to its source Store, domain identity when Genesis exists,
and captured revision. It refuses cross-Store reuse, mixed projection contexts,
or a Store that changes during or after capture. It retains source record order
and actual revisions and does not synthesize historical revisions. Existing
read-facade data is immutable and exposes no Store writes. Existing
Contract selection, card sorting, complete-history validation, and status
precedence remain in place. `show` and `result` keep their existing query paths.

This removes repeated persisted runtime-record scans from aggregate card
projection. It does not cache across commands, filter history before canonical
validation, or promise that the remaining in-memory projection work is linear.

## Verification

An installed old/new comparison used the same signed 100- and 300-Contract
fixtures. `task list`, `task board` and `task stats` returned identical JSON in
both installations, including the complete 100- and 300-task lists. The query
snapshot retained 18 SQLite tables, schema metadata and runtime revisions 501
and 1,501 without changing any of them.

In the measured board query, persisted runtime scans fell from 707 scans and
251,003 returned records to 7 scans and 503 returned records for 100 tasks,
and from 2,107 scans and 2,253,003 returned records to 7 scans and 1,503 returned
records for 300 tasks. These counts measure records returned by the Store scan
method, not physical SQLite row visits or CPU work. Baseline sampling partly
overlapped the full suite, so elapsed times do not establish a controlled
latency improvement.

The full test suite, Ruff, formatting, package build and an external installed
wheel check passed. The fixture run used no Worker or provider. This evidence
does not establish performance for descendant-heavy or otherwise different
task graphs.
