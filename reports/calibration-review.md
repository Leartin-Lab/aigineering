# Case: an unqualified calibration report

## Task

The [synthetic calibration recipe](../examples/thermal_calibration/CLI.md) asks
for normalization, drift analysis, exact evidence references and independent
acceptance. Its deterministic oracle can recompute the numerical report.

## Recorded outcome

Three bounded real-model trials of source candidate
`1055c0e95a504ac8e6e44a3b1d7a6fb8e83e4506` produced **zero qualified deliveries**.
Two generated numerically correct reports with the required tool observations,
but the verifier instructions did not satisfy the declared JSON protocol. No
attestation or qualification was committed, and protected export correctly
refused an accepted-result export.

Recovery was limited by the available work allowance and provider request budget.
Publishing a recovery did not by itself make the run successful. A separate
example-adapter mismatch for failure-task output names was subsequently fixed;
that fix did not retroactively qualify these trials.

## What this demonstrates

Correct calculations and runtime protocol completion are different from an
accepted delivery. Exact output/acceptance boundaries contained the failure,
while model action discipline and verifier task preparation remained practical
obstacles. This is a retained failure case, not a reliability estimate.

The current source and configuration have not been live-qualified by this
historical result. The summary does not establish provider quality, billing,
long-run safety or an independently replayable acceptance certificate.
