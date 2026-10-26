# Change 131: Bounded calibration Researcher profile

Status: Implemented. Component acceptance is complete; combined artifact
verification and its exact evidence are tracked in report 116.

The G0c thermal-calibration example now binds the Researcher role to
`thinking_mode="disabled"`, matching the Planner. The meter profile identity
is `g0c-runtime-v2` and its fingerprint records that exact mode. Both roles
still use the same process-local G0a meter, selected `deepseek-flash` alias,
ordinary `LLMWorker`, existing tools, zero retries, 8,192 per-request output
cap, and current configured attempt/reservation/deadline values. The described
candidate profile is 8 attempts, 65,536 aggregate output reservations,
180-second cooperative deadline, and 60-second request timeout; configuration
environment values remain selectable and a changed value is a different
frozen run profile.

The shared Skill adds a planning reference for the ordinary Planner stages, two
tool observations, their continuations, and final outputs, scoped explicitly to
the selected frozen G0c metered candidate. The ordinary example preset is
distinct, and configurable operational limits must be read from the exact
selected frozen configuration; the Skill does not establish active limits. The model continues to
choose ordinary runtime tasks dynamically. Causal task allowance remains
separate from provider permits and output reservation. The guidance preserves
the full `calibration_report` and `calibration_evidence` contract and the
existing deterministic independent verifier. It does not add a scheduler,
fixed domain task graph, allowance transfer, meter reset, or recovery grant.

The changed thinking profile is a bounded hypothesis, not a demonstrated fix
or causal explanation. All three prior Runtime trials remain failures: zero
were completed and zero were independently accepted. The independent G0d audit
traced the first two refused recovery publications to exhausted causal
allowance and the third trial's three claimed recovery tasks to the already
exhausted process meter. Those failure paths are not repaired by this change.
Reported usage does not reveal a reasoning-token split; billing cost and
immutable model revision remain unknown.

## Verification record

Author checks on the isolated candidate pass: 43 no-network/no-Redis tests
cover the existing meter and role/payload owner plus offline preparation and
Redis guards; five ordinary CLI cases pass with fake provider transport and
the explicitly assigned native Redis lease; Ruff check and format checks pass
for the four changed Python files. The ordinary CLI cases use real Runtime
claims, commitment and qualification; they are not described as no-network
tests and their provider replies are fixtures, not live provider evidence.

Earlier test failures and the corrected intermediate assertion are retained
with their original exits in the private author evidence. This package made
no live provider request, read no real credential, and did not run the full
suite, build a package, or install from the source tree. These author checks
do not constitute independent acceptance, live-provider evidence, or
stable-release qualification.
