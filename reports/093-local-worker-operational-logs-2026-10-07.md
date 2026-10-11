# Bounded local Worker operational logs: development verification

Date: 2026-10-07. Source version: **0.5.13.dev0**.
This report records the local operational-log slice. It is not a stable-release,
publication, full-transcript or real-provider qualification.

## Implemented behavior

`aig task logs CONTRACT_ID --claim CLAIM_ID` reads actual local runtime invocation
and built-in LLM retry/failure observations. `--store PATH` selects the Store's
adjacent spool; `--limit`, prefix-bound `--cursor` and bounded `--follow` provide
incremental inspection. JSON follow emits independent compact objects and a
final status/cursor. Timeout or interruption stops only the reader.

Quick, pool, target and configured/Fleet runs enable capture by default. One
invocation-scoped observer restores after failures and masks nested Engine calls.
The reader checks exact diagnostics claim proof and the matching header. Redis
is required; SQLite alone remains signed fact/claim authority. Logs do not grant
readiness, authority, Candidate acceptance or output qualification. Invocation
finish precedes signing/submission; EOF is not a task terminal.

Private exclusive files have strict typed schemas, prefix hashes and per-claim
256 KiB/512-event and spool64 MiB bounds. The chain detects accidental mutation,
not authentication. Files are not overwritten on restart or pruned automatically.
Capture failures produce separate warnings without changing normal execution.
Quota-lock acquisition is nonblocking; synchronous filesystem operations can add
latency, including fsync. Missing capture has unknown cause. Custom Worker stdout/
stderr, tool/Asset bodies, raw output, prompts, URLs, credentials, arbitrary
exceptions, full transcripts and remote sources are outside capture coverage.

## Source and independent evidence

The frozen runtime has 214 Python source files, map SHA256
`0a82346997ba34d28cdc5966ae1beb1b97406a34c1a226c0ebd82b7bff59f271`.
The full-suite snapshot has 547 source/config/test/helper files, map SHA256
`abf47101b42d1e3588fdbe83de103d21ca7d512539e22b41fd0c2ec67a53d4d7`.
After that run, only the installed helper changed; it is not executed by pytest.
Its final SHA256 is
`7033332144d3ea3e96ccdf4ff22295a3a279ec78f96088c21ca1ef7faadb5575`.
The installed source/config/test/helper map has 547 files, SHA256
`1f1a9baa84628863ef0df106774334f879bb93d105006fd2dc6ef9541a967031`;
runtime and test bytes remain unchanged from the full-suite snapshot.

- Root serial full suite: **2228 passed**, six existing dependency deprecation
  warnings, 111.85 seconds, on isolated temporary Stores and real Redis.
- Root Ruff and format checks passed; **470** source/test files checked.
- Independent affected-file review/tests: **31 passed**. A later test-only fixture
  delta independently passed **20** complete-file tests. Counts overlap and are
  not added to the full-suite count.
- A separate independent probe uses a real ThreadPoolExecutor, capture wrapper
  and CaptureWriter.close with delayed failing fsync. It confirms Fleet collects
  close warnings after slots join. Its Store/claim/runtime/projection are faked;
  it does not establish operational claim or SQLite integration.

The initial full run retained **4 failures / 2224 passes**. Existing fixtures
patched the old execution owner or omitted the new capture_warnings field.
Test-only repairs preserve priority, claim, delegation and result assertions;
production code was unchanged. Earlier sandbox TCP failures are separate from
product behavior. Some author-side failures have only recovered tool transcripts,
with no saved original byte streams; this provenance gap is not reconstructed
as original evidence.

## Installed-artifact acceptance

The packaged helper exercises actual ordinary signed Local WorkerHost calls
using an injected LLM transport, with a real first retry branch followed by a
valid result. It never prewrites successful capture rows. Its corrected controls
cover a custom Store, bounded read/follow, shared Host concurrency, privacy,
reopen/rebuild, corruption and storage failures.

Root installed a fresh wheel outside the source tree using an offline dependency
wheelhouse and real isolated Redis. **86 retained phases passed** with the final
independently reviewed packaged helper. The wheel SHA256 is
`72bb835cadf055c5ed084236fdffcaa62d36be910e1425acd4526a62b9bf75cb`.
The baseline capture contains actual `invocation.started`, `provider.retry`,
`candidate.returned` and `invocation.finished` events, read over four pages.

Controls passed for a custom configured Store without default/wrong-Store
fallback, exact claim/cursor scope, live follow, timeout, interruption, symlink
conflict, corruption, incomplete tails and resumed cursors. Two overlapping
invocations share one Host/adapter and retain distinct actual retry statuses
503/429. Quota and permission denial preserve signed task execution and produce
capture warnings; missing capture and required Redis outage fail observably.
Prompt/provider/output privacy markers are absent from captured bytes and views.

All **18 SQLite tables**, including sqlite_sequence, retain their fingerprints
across read/follow/error paths, read-only close/reopen and Redis/query rebuild.
Separate SQLite materialization maintenance preserves immutable records and
the canonical digest before/after repeated rebuilds. It normalizes display
timestamps in worker_claims/worker_registrations, so those raw rows differ;
independent hashes verify every other column, including actor_id/key_id, remains
unchanged. This does not claim all SQL rows are byte-identical after that
maintenance operation. The registration digest in this frozen runtime omits
these two identity columns; the separate
[verification correction](094-registration-identity-materialization-digest-2026-10-07.md)
records its subsequent repair.

Four earlier installed runs remain retained failures: table inventory excluded
sqlite_sequence; a configured-run JSON document was parsed as JSONL; then two
overbroad raw-row assertions confused SQLite projection maintenance with the
required read/Redis neutrality. Additional pre-execution helper defects and
static-control failures are retained separately. Later success does not turn
those attempts into passing evidence. No runtime code changed during these
helper corrections.

There were **zero real-provider generation requests** in this slice. The accepted
[real repository report](091-real-repository-report-acceptance-2026-10-07.md) and
[context inspection](092-task-context-view-2026-10-07.md) bind earlier snapshots.

## Remaining scope

Full transcripts, remote log sources and explicit retention remain future work.
Task attachment links, board management, persistent/scheduled dispatch,
notifications and whole-objective stable gates remain open. No commit, remote
branch update, tag or package publication is implied.

See [change 063](../changes/063-local-worker-operational-logs.md),
[ADR-044](../docs/adr/ADR-044-local-worker-operational-logs.md) and
[usage](../docs/reference/task-board.md#inspect-local-worker-logs).
