# Declarative examples

The two new case studies below exercise signed root tasks, planner-created
child Contracts, claimable Worker packages, and independent attestation. Their
oracle files are human review aids and are deliberately excluded from runtime
inputs. The linked report records their actual manual outcomes.

## Shared execution entry

The complete AI4S, report consistency, claim-evidence and guarded claim-review
recipes each provide `run.toml`. Start with
`aig -C WORKSPACE run --config /absolute/path/to/run.toml`, then continue the
returned task with the same configuration and `--resume TASK_ID`. Method examples
keep import/test/assessment preparation, followed by the same configured run
entry for their existing task IDs. Skill-only packages remain reusable resources.
See [configured runs](../docs/reference/configured-runs.md).

[`cli_research`](cli_research/README.md) provides a bounded synthetic measurement
report with runtime planning, two chained tools, explicit envelope validation
and a separate authorized verifier. It includes no example-owned task driver.

## New diagnostic case studies

- [`report-consistency`](report-consistency/README.md) checks a corrected sales
  report against a separate checking standard; its recorded manual review
  passed.
- [`claim-evidence-review`](claim-evidence-review/README.md) reviews a
  synthetic research summary for evidence support and causal overclaiming; four
  protocol-completed attempts all failed a manual semantic criterion.

These outcomes show the runtime boundary and the limits of LLM semantic
verification. They do not establish reliable autonomous correctness.

The experimental [`guarded-claim-review`](guarded-claim-review/README.md)
adapter composes deterministic assessment and gating, one revision generation,
semantic review, and dedicated acceptance.
[Report 065](../reports/065-guarded-claim-review-2026-09-13.md) records two
qualified live runs and six preserved development failures; the final run leaves
no active descendant Contracts. These are development cases, not a reliability
benchmark.

## Deterministic validation methods

- [`thermal_calibration`](thermal_calibration/README.md) provides versioned
  synthetic sensor data, bounded normalization/analysis tools and an independent
  exact-rational report oracle. Its [CLI recipe](thermal_calibration/CLI.md)
  configures ordinary planning, tool continuation and a separate deterministic
  verifier. Protocol fixtures exercise the execution paths; real provider
  evaluation and the direct Harness comparison are still pending.
- [`claim-validation`](claim-validation/README.md) packages exact original-text
  and explicit evidence-binding checks with local Workers and a blocking gate.
  These checks do not validate free-text semantic support.

## Existing examples

- [`ai4s`](ai4s/README.md) runs the literature retrieval and synthesis example.
- [`method-governance`](method-governance/README.md) demonstrates evaluated
  method package reuse with its explicit fixture/demo workflow.

The same-model Worker independence limit is stated in each applicable new case
README. The shared evidence record is
[`reports/063-runtime-native-cases-2026-09-13.md`](../reports/063-runtime-native-cases-2026-09-13.md).
