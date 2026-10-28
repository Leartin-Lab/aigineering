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
mkdir -p workspaces/report-consistency
aig -C workspaces/report-consistency run --config /path/to/aigineering/examples/report-consistency/run.toml \
  --wait-timeout 300 --json
# Use the returned contract_id for continuation or audit:
aig -C workspaces/report-consistency run --config /path/to/aigineering/examples/report-consistency/run.toml \
  --resume TASK_ID --wait-timeout 300 --json
aig -C workspaces/report-consistency task audit TASK_ID --json
```

Replace `TASK_ID` with the root Contract ID returned by `aig run`.
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

This fixture has a separate manual review aid. A passing fixture review would
not establish reliable autonomous checking of arbitrary reports.

The preset paths are relative to `run.toml`; its database is relative to the
selected workspace. Use a fresh workspace for an independent run; a new start
refuses historical outputs. Resume does not reload inputs or reset a failed root.
See [configured runs](../../docs/reference/configured-runs.md). The entrypoint
migration has deterministic verification; the earlier linked live reports do not
constitute new provider execution of this preset.
