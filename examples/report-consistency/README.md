# Declarative report consistency example

This fixture uses the ordinary runtime path: one signed root Contract asks a
Worker to `/plan`, the planner publishes child Contracts, Workers execute those
children, and an independently routed verifier attests the final report. There
is no hand-written DAG or business orchestration script.

The source data is a small monthly sales ledger. `draft-report.json` contains a
deliberately incorrect total and one incorrect month. `check-standard.json` is
the independent checking standard; it is disclosed to checking and verification
tasks, while the root output must be a corrected, structured audit report.

From an empty working directory:

```bash
export DEEPSEEK_API_KEY=replace-me
export AIGINEERING_REPORT_CONSISTENCY_DIR=/path/to/aigineering/examples/report-consistency

aig domain init
aig skill load "$AIGINEERING_REPORT_CONSISTENCY_DIR"
aig asset add --name report_source \
  --content-file "$AIGINEERING_REPORT_CONSISTENCY_DIR/source-data.json" --json
aig asset add --name draft_report \
  --content-file "$AIGINEERING_REPORT_CONSISTENCY_DIR/draft-report.json" --json
aig asset add --name check_standard \
  --content-file "$AIGINEERING_REPORT_CONSISTENCY_DIR/check-standard.json" --json
aig task create --name report_consistency_audit \
  --description-file "$AIGINEERING_REPORT_CONSISTENCY_DIR/task-description.txt" \
  --input report_source --input draft_report --input check_standard \
  --activation 'report_source AND draft_report AND check_standard' \
  --output audit_report --budget 24 \
  --label _skill_content_report_consistency \
  --requires-capability planning --worker-pool reasoning \
  --delegate-capability report.extract \
  --delegate-capability report.check \
  --delegate-capability report.revise \
  --delegate-capability report.replan \
  --delegate-capability report.verify \
  --delegate-pool economy --delegate-pool reasoning --delegate-pool verification \
  --acceptance-policy "$(cat "$AIGINEERING_REPORT_CONSISTENCY_DIR/acceptance-policy.json")" \
  --json

aig fleet run --config "$AIGINEERING_REPORT_CONSISTENCY_DIR/workers.toml" \
  --task TASK_ID --wait-timeout 300 --json
aig task audit TASK_ID --json
```

Replace `TASK_ID` with the root Contract ID returned by `aig task create`.
The fleet uses `deepseek-flash` at the configured DeepSeek endpoint and
reads only `DEEPSEEK_API_KEY` from the environment. Never put the key in an
Asset, task description, or checked-in file.

The independent verifier must check the exact disclosed source, draft, standard,
and audit report Assets before attesting the root Contract. The deterministic
oracle for a human review is kept separately in `ORACLE.md`; it is not loaded as
an Asset or supplied to any Worker. Independent routing provides a separate
verification role and signing identity, but the checked-in fleet uses the same
model family for its Workers. This limits the independence claim to runtime
authority, disclosure, lineage, and attestation boundaries, rather than an
independent model opinion.

The manual review for this case passed in the recorded run. See the
[`reports/063-runtime-native-cases-2026-09-13.md`](../../reports/063-runtime-native-cases-2026-09-13.md)
for the measured execution details; that result is evidence for this fixture,
not a guarantee for arbitrary reports.
