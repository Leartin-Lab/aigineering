# Pool and target run failure diagnostics

Pool (`aig run --once`) and target (`aig run --task`) failures now include the
same additive `worker_failure_details` projection used by local Fleet. Details
name the actual failed task and claim only when the shared exact-claim reader
proves the failure and release at one Store revision. Target status continues
to describe the requested root. If the proof is unavailable or inconsistent,
the result omits identifiers and returns `diagnostic_unavailable`.

Human output uses the same safe formatter. Arbitrary exception text is not
returned in the result's error field. The fixed “claim was released” hint is
shown only when exact diagnostics prove the release. Rendering does not create
facts, retry work, reset claims, or schedule recovery. Configured/Fleet behavior
is unchanged; quick/demo invocation reporting is outside this change.

The integrated suite passed 2490 tests, with six dependency warnings. Ruff,
format checks and build passed. Source, wheel, source distribution and the
external installation contained the same 225 runtime modules. Installed pool
CLI checks covered JSON and human output using native Redis and two isolated
HTTP 401 fixture cases, each making one local request. Exact failure and release
proofs matched the displayed diagnostics; read-only inspection preserved the
original stores. A separate installed Fleet fixture checked shared reporting,
reopen and private-backup reconstruction. The initial two full-suite regressions
and rejected verification-script preparations were retained and corrected.

These are bounded fixture checks with no real provider call. They do not prove
current provider productivity or close the complete lifecycle release gates.
