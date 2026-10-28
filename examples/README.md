# Examples

Start with [CLI research](cli_research/README.md): one frozen input, dynamic
ordinary planning, two tools, a report and policy-bound acceptance. The example
adapter is loaded explicitly from the checkout; it is not included in the wheel.

| Example | Purpose |
| --- | --- |
| [CLI research](cli_research/README.md) | Small measurement report with tools and a separate verifier. |
| [Report consistency](report-consistency/README.md) | Check a report against explicit source and criteria. |
| [Claim-evidence review](claim-evidence-review/README.md) | Review synthetic claims and evidence bindings. |
| [Guarded claim review](guarded-claim-review/README.md) | Experimental deterministic checks plus semantic review. |
| [Calibration](thermal_calibration/README.md) | Synthetic data, deterministic tools and exact-rational oracle. |
| [Claim validation](claim-validation/README.md) | Original-text and explicit evidence-binding checks. |
| [AI4S](ai4s/README.md) | Runtime-compiled literature/data-profile integration. |
| [Method governance](method-governance/README.md) | Evaluated method packages with explicit fixture preparation. |

Complete recipes provide `run.toml`; start with `aig -C WORKSPACE run --config
/absolute/path/to/run.toml`, then use the returned task ID and the same config
with `--resume TASK_ID`. Skill-only packages are resources, not standalone runs.
See [configured runs](../docs/reference/configured-runs.md).

Configuration availability is not a claim of general live reliability. Read the
selected [case reports](../reports/README.md), including an unqualified result.
Input fixtures are synthetic; acceptance policies and explicit tool permissions
remain authoritative. An oracle or answer key must not be disclosed as an input
to the Worker it is evaluating.
