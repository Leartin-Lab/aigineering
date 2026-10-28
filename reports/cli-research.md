# Case: bounded local CLI research

This selected case used the installed **Aigineering 0.5.14** wheel
(`ef35a513fdfd975731252e5d52c4f84a49da148021bc2aa88d288380e18f480a`), the
original [CLI research recipe](../examples/cli_research/README.md) from the source
distribution, and `deepseek-flash`.
The three predeclared scenarios were each run once: a quick goal, the configured
research task, and the same configured task with a one-time first-tool failure.
All three roots reached `complete`; this is a bounded case, not a success rate.

The input was a synthetic sample of 12, 14, 16, and 18 °C. Each report gave four
samples, a mean of 15 °C / 59 °F, and limitations. In the configured scenarios,
the report referenced the exact
sample Asset and two committed tool observations. A different signed verifier
accepted the exact report under the root's policy; canonical root qualification
was present and protected export bytes matched the accepted Asset.

The configured success retained two malformed planning actions rejected at the
Worker boundary before commitment, then used two ordinary recovery tasks: the
first failed and the second completed. The injected-failure scenario retained
one `tool_schema_invalid` failure; a newly published recovery task was actually
claimed, submitted, and completed before delivery. Recovery receipts alone do
not establish qualification. Export-overwrite refusal was checked. Resuming an
already complete root caused no provider call, new claim, or usage. Closed-copy
strict rebuild/reopen checks, including Worker identity, matched while the
original Store bytes remained unchanged.

Validation was reported separately: the full source suite passed 3,659 tests,
with no skips and six existing third-party deprecation warnings. The installed
artifact had 117 passing fixture tests; these are deterministic checks, separate
from the three real-model scenarios.

An earlier 0.5.14 candidate (wheel SHA-256
`4c6d3c70e7040fd8792b7c612af069b369c727a29526b03a1c43c13dfb86d167`)
had a different outcome: its normal configured scenario timed out after a draft used its planning
stage ID as the root acceptance target. The compiler carried that ID forward;
canonical `asset.attest` rejection correctly refused the undeclared target. A
receipt recovery completed, but the root remained unqualified. This failure is
retained and is not replaced by the later candidate's result. Earlier
calibration trials also remain **0 qualified out of 3**; this case does not
revise that history or the unexplained Fleet stop.

These summaries describe selected outcomes for named artifacts. They are not a
general reliability estimate or an independently replayable evidence bundle.
They do not establish arbitrary-task success, lower cost, independent knowledge,
long-run stability, process isolation, or exactly-once external effects.
