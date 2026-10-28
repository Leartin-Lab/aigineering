# Case: bounded local CLI research

This selected case used the installed **Aigineering 0.5.14** wheel (SHA-256
`6240012a9d697e88c8a94580de6af7e96c4af41cc2542e051fe1c2b1292af52e`), the
original [CLI research recipe](../examples/cli_research/README.md) from the
source distribution, and `deepseek-flash`.
Three predeclared scenarios were each run once: a quick goal, configured
research, and configured research with a one-time first-tool failure. All three
root tasks reached `complete`; these selected runs are not a success rate.

The input was four synthetic samples (12, 14, 16, and 18 °C). The reports gave
the mean (15 °C / 59 °F) and limitations. The configured cases referenced the
exact sample Asset and two committed tool observations. A distinct signed
verifier accepted the exact report under the root policy; canonical root
qualification and matching protected export bytes were checked. Export
overwrite refusal was also checked.

The normal configured case retained a rejected task delegation. Recovery work
and tool continuations ran before root qualification; the recovery task itself
was cancelled during root completion. In the injected-failure case, a malformed
planning action and a `tool_schema_invalid` tool failure were retained, with
ordinary recovery work executed. A completed-root resume made no provider call,
new claim, or usage. Closed-copy strict rebuild/reopen, including Worker
identity, matched, and the original Store bytes remained unchanged.

The 3,660-test source run and subsequent affected-test recheck passed, with no
skips and six existing third-party deprecation warnings. The installed artifact
had 118 passing fixture tests. These deterministic checks are separate from the
three real-model scenarios. Cross-platform results are available in the
[release checks](https://github.com/Leartin-Lab/aigineering/pull/4/checks).

An earlier 0.5.14 candidate (wheel SHA-256
`4c6d3c70e7040fd8792b7c612af069b369c727a29526b03a1c43c13dfb86d167`)
timed out in its normal configured scenario after a draft used its planning
stage ID as the root acceptance target. Canonical attestation correctly
rejected the undeclared target; receipt recovery did not qualify the root. That
failure remains part of the evidence. Earlier calibration trials remain **0
qualified out of 3**, and the unexplained Fleet stop remains open.

This is a bounded selected case, not a general reliability estimate or an
independently replayable evidence bundle. It does not establish arbitrary-task
success, lower cost, independent knowledge, long-run stability, process
isolation, or exactly-once external effects.
