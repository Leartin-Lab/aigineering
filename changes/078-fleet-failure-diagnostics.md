# Fleet failure diagnostics

Bounded Fleet and configured-run results now expose an additive
`worker_failure_details` field. A caught invocation failure includes its task,
claim, package, Worker, epoch and failure/release fact references only when the
shared diagnostics owner proves that exact claim at one Store revision. Missing
or inconsistent history returns a fixed `diagnostic_unavailable` entry without
claim identifiers. Unexpected slot exceptions discovered after bounded slots
join return an unbound `slot_error`; arbitrary exception text is omitted.

The existing `worker_errors` field and persistent Fleet tuple remain compatible.
Bounded and persistent slots use the same safe invocation-failure formatter.
Late bounded future failures are collected after executor join without changing
the fact-derived terminal or timeout result. These observations do not create
failure or recovery facts, retry work, or establish business completion.

Focused tests cover exact diagnostic bindings, bounded and persistent Fleet
formatting, late joined-future failures, and configured CLI output. The integrated
suite passed 2484 tests after an earlier run exposed one outdated internal test
call; that failure is retained. A source-external installed artifact with real
Redis completed one explicit fixture-worker failure, exact claim inspection,
reopen and private-backup reconstruction. Read queries preserved the original
18 tables' contents. These checks make no provider or whole-release claim.
