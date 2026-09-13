# Declarative examples

The two new case studies below exercise signed root tasks, planner-created
child Contracts, claimable Worker packages, and independent attestation. Their
oracle files are human review aids and are deliberately excluded from runtime
inputs. The linked report records their actual manual outcomes.

## New diagnostic case studies

- [`report-consistency`](report-consistency/README.md) checks a corrected sales
  report against a separate checking standard; its recorded manual review
  passed.
- [`claim-evidence-review`](claim-evidence-review/README.md) reviews a
  synthetic research summary for evidence support and causal overclaiming; four
  protocol-completed attempts all failed a manual semantic criterion.

These outcomes show the runtime boundary and the limits of LLM semantic
verification. They do not establish reliable autonomous correctness.

## Existing examples

- [`ai4s`](ai4s/README.md) runs the literature retrieval and synthesis example.
- [`method-governance`](method-governance/README.md) demonstrates evaluated
  method package reuse with its explicit fixture/demo workflow.

The same-model Worker independence limit is stated in each applicable new case
README. The shared evidence record is
[`reports/063-runtime-native-cases-2026-09-13.md`](../reports/063-runtime-native-cases-2026-09-13.md).
