# Synthetic calibration drift review

Work only from the disclosed `calibration_case` and
`calibration_standard` Assets. These inputs are synthetic and do not establish
real-world sensor quality or regulatory compliance. The Skill gives procedure
and action-format context; labels do not grant authority.

## Resource guidance for the selected frozen G0c metered candidate

This guidance applies only when the run selects the exact frozen G0c metered
candidate whose worker configuration and meter profile were reviewed together.
The candidate's root Contract has a 32-unit causal allowance, separate from
its process-local provider meter. In that candidate, Planner and Researcher
share at most 8 provider attempts, with an 8,192-token output cap per request,
a 65,536-token aggregate output reservation ceiling, a 180-second cooperative
deadline, a 60-second request timeout, and zero retries. These are limits of
that frozen candidate, not properties guaranteed by this shared Skill, token
billing, or a task allowance; each physical attempt consumes a permit. A new
process is a new meter epoch, not budget continuation for an earlier process.

The ordinary example preset and configurable profiles are distinct. Do not
apply the candidate's figures to them. A Skill cannot determine active
operational limits: establish those from the exact selected and frozen run,
worker, and meter configuration before planning or reporting limits.

Use costs only as planning guidance: the ordinary Planner root and its existing
staged turns, followed by Researcher turns for the two required tools and final
outputs, have a seven-request reference path if every turn returns in one
physical request. The model still chooses its ordinary task decomposition; do
not force a task graph or count. The existing tool owner requires two causal
units for one tool child and its continuation. Two serial tool children plus a
positive-budget final continuation therefore have a three-unit lower bound
for that modeled path only. This is not a sufficient whole-run allowance or a
recovery reserve. The deterministic verifier is an ordinary funded task but
does not consume a provider permit.

Plan recovery only when the exact parent Contract has available causal
allowance and the same process still has provider permits for any model
continuation. Do not increase a Contract budget, borrow allowance from another
parent, or create a new process epoch to disguise exhaustion. If the ordinary
owners cannot fund necessary work, report the actual blocker or fail with a
concrete reason; scheduled recovery is not completion.

Begin the root Contract with `/plan`. Choose the task decomposition yourself
using ordinary planning stages. Compile ordinary research work requiring
`calibration.research` in pool `research`, with budget for both required tool
calls and their continuations as justified by existing owners. Add a separate verifier that
requires `calibration.verify` in pool `verification`. Both must retain this
Skill label. The verifier consumes the two exact source inputs, report, and
business evidence; its description must bind the exact root Contract ID and
both source Asset IDs from the planning stage.

Research must call `normalize_calibration` with the exact disclosed case and
standard, then call `analyze_calibration` with the complete `result` JSON from
the successful normalization observation. Do not round or edit intermediate
results. Wait for both successful committed observations before publishing the
final outputs. A failure does not count as a successful observation; follow an
authorized recovery if available, otherwise return `/fail` with a concrete
reason. Do not claim that scheduled recovery has already run.

Publish `calibration_report` as the exact report returned by
`analyze_calibration`. Publish `calibration_evidence` as JSON with exactly
these keys: `schema` (`thermal-calibration-evidence-v1`),
`calibration_case_asset_id`, `calibration_standard_asset_id`,
`observation_asset_ids`, and `observations`. The two observation IDs must be
distinct. `observations` contains, in tool order, objects with exactly
`asset_id` and the complete `observation` JSON object for
`normalize_calibration`, then `analyze_calibration`. Copies make the content
available to the verifier; they do not prove the observations' signed runtime
history.

The verifier recomputes the report from the original case and standard with an
independent exact Fraction oracle, checks complete evidence and exact input
Asset IDs, and only then submits `/attest` for the exact root ID and report
Asset ID with both source Asset IDs in `evidence_asset_ids`. If any check fails,
return `/fail`. A correct report with `within_tolerance=false` is still a
correct report; it describes a case outside the synthetic threshold.

The verifier Contract must have exactly one output, `calibration_receipt`, and
exactly these four input roles: `calibration_case`, `calibration_standard`,
`calibration_report`, and `calibration_evidence`. Give it capability
`calibration.verify` in pool `verification`. Its description is JSON with
exactly these five keys and values: `schema` is
`thermal-calibration-verifier-target-v1`; `root_contract_id` is the exact root
Contract ID (not the compile-stage Contract ID); `calibration_case_asset_id`
and `calibration_standard_asset_id` are the exact disclosed source Asset IDs;
and `policy_version` is `synthetic-temperature-calibration-v1`.
