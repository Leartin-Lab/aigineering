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
mkdir -p workspaces/claim-evidence-review
aig -C workspaces/claim-evidence-review run --config /path/to/aigineering/examples/claim-evidence-review/run.toml \
  --wait-timeout 300 --json
# Use the returned contract_id for continuation or audit:
aig -C workspaces/claim-evidence-review run --config /path/to/aigineering/examples/claim-evidence-review/run.toml \
  --resume TASK_ID --wait-timeout 300 --json
aig -C workspaces/claim-evidence-review task audit TASK_ID --json
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

The preset paths are relative to `run.toml`; its database is relative to the
selected workspace. Use a fresh workspace for an independent run; a new start
refuses historical outputs. Resume does not reload inputs or reset a failed root.
See [configured runs](../../docs/reference/configured-runs.md). The entrypoint
migration has deterministic verification; the earlier linked live reports do not
constitute new provider execution of this preset.
