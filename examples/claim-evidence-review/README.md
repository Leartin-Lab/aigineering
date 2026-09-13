# Declarative claim-evidence review example

This example exercises the runtime's ordinary planning and fleet execution on
a synthetic research summary containing an association, a causal overclaim,
and a population-wide overclaim. The workflow extracts evidence, checks each
claim, revises the summary with calibrated language, and asks an independent
verifier to attest the exact report. The input deliberately contains source
excerpts and limitations but no answer key.

The oracle is documented separately in `ORACLE.md` for a human reviewer. It is
not loaded as a task Asset and is not available to workers during execution.

From an empty working directory:

```bash
export DEEPSEEK_API_KEY=replace-me
aig domain init
aig asset add --name research_result \
  --content-file /path/to/aigineering/examples/claim-evidence-review/research-result.json --json
aig task create --name claim_evidence_review \
  --description-file /path/to/aigineering/examples/claim-evidence-review/task-description.txt \
  --input research_result --activation research_result \
  --output review_report --budget 18 \
  --requires-capability planning --worker-pool reasoning \
  --delegate-capability claim.review.extract \
  --delegate-capability claim.review.check \
  --delegate-capability claim.review.revise \
  --delegate-capability claim.review.verify \
  --delegate-pool economy --delegate-pool reasoning --delegate-pool verification \
  --acceptance-policy "$(cat /path/to/aigineering/examples/claim-evidence-review/acceptance-policy.json)" \
  --json
aig fleet run --config /path/to/aigineering/examples/claim-evidence-review/workers.toml \
  --task TASK_ID --wait-timeout 300 --json
aig task audit TASK_ID --json
```

The checked-in worker template uses `deepseek-flash` at the DeepSeek
compatible endpoint. The API key is read only from `DEEPSEEK_API_KEY`.

`ORACLE.md` is for a human reviewer only: it is not loaded as an Asset and is
not supplied to any Worker. The verifier has a separate capability, pool, and
signing identity, but the checked-in fleet uses the same model family for all
LLM Workers. The resulting independence claim concerns separate runtime
authority and evidence checks, rather than independent model training or
opinions.

The recorded case completed the runtime protocol in four attempts, but all
four failed at least one manual semantic criterion. See the
[`reports/063-runtime-native-cases-2026-09-13.md`](../../reports/063-runtime-native-cases-2026-09-13.md)
for the failure details. This is a diagnostic and boundary case, not evidence
of reliable autonomous semantic verification.
