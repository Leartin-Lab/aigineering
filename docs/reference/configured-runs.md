# Configured runs

Start an explicit workload with the same `aig run` command used for a quick goal:

```bash
mkdir -p workspaces/report
aig -C workspaces/report run --config /absolute/path/to/examples/report-consistency/run.toml \
  --wait-timeout 300 --json
```

Set the provider environment variables named by the Worker profile before
launch. The preset publishes inputs, Skill Assets and one root Contract, then
uses the canonical local Fleet. It never supplies an answer key or a domain DAG.
The result includes `contract_id`, projected `status`, `completed`, `timed_out`
and `worker_errors`. Completion satisfies the Contract policy; it does not prove
general semantic correctness. `--json` emits one result object, not a streaming
format shared with another harness.

To continue an existing root or a separately published recovery task:

```bash
aig -C workspaces/report run --config /absolute/path/to/examples/report-consistency/run.toml \
  --resume TASK_ID --wait-timeout 300 --json
aig -C workspaces/report task audit TASK_ID --json
```

`--task` remains an alias for target selection; do not combine it with
`--resume`. With a preset, resume reads only its Fleet reference; deleted input
or Skill files do not change the frozen task. Existing Worker TOML files are also accepted.
See [run cycles and retry boundaries](task-board.md#run-cycles-and-retry-boundaries)
for target-loop stopping and the distinction between provider retries and task recovery.
Without `--config`, resume uses the existing single-worker target
mode and its provider options. Resume never resets a claim or failed task, and
does not automatically run a recovery successor. A terminal target stays
terminal; select the actual recovery ID when recovery is needed.

`-C`/`--workspace` selects an existing working directory before the subcommand.
Relative configuration paths are resolved there. Preset resource paths and the
referenced Fleet file are relative to the preset file; Fleet `db_path` remains
relative to the workspace, matching existing Fleet behavior. Configured runs
require a non-empty persistent database path and reject SQLite `:memory:`.
Workspace selection is a path convention, not a filesystem sandbox. Factories must be importable by
the installed Python environment or an explicit checkout `PYTHONPATH`.

## Preset format

```toml
[run]
fleet = "workers.toml"
skills = ["skills/report"]

[[inputs]]
name = "source"
path = "source.md"

[task]
name = "report"
description = "Produce a report from the disclosed source."
inputs = ["source"]
activation = "source"
outputs = ["report_output"]
budget = 8
labels = ["_skill_content_report"]
worker_capabilities = ["report.write"]
worker_pools = ["reasoning"]
```

Use `description_file` instead of `description`, and `acceptance_policy_file`
instead of an inline `acceptance_policy` table, when useful. The task fields
also include `tool_scope`, `delegation_capabilities`, `delegation_pools`, and
`sensitive_input_policy`. These retain ordinary Contract semantics; neither
Skills nor labels grant tool or publication authority.

For independent policies that require exact source evidence, list declared input
names in `task.acceptance_evidence_inputs`. The launcher binds the accepted input
Asset IDs to `acceptance_policy.evidence_asset_ids` before publishing the root.
Do not also supply literal evidence IDs in that policy.

Unknown fields, invalid types, missing or invalid UTF-8 resources, duplicate
inputs, missing labels and invalid Contracts are errors. Input files are bounded
at 1 MiB each. Provider keys are referenced only by the existing Fleet
`api_key_env` field. `response_format = "json_object"` selects the provider's
JSON action wire mode for an LLM profile and cannot be combined with a
`tool_registry`; ordinary parsing and commitment still apply. Configuration mode rejects goal, mock, tool and provider flag overrides;
edit the explicit profile instead.

A new preset launch refuses roots and output slots present at its startup check.
That check is an application guard, not an atomic output namespace reservation.
Use distinct workspaces for independent concurrent runs. Use `--resume` to continue
that fact domain or a fresh workspace for an independent run. Setup publication
and execution are separate signed transactions; a failed start can leave
accepted setup evidence. Task audit and the runtime records remain authoritative.

## Example coverage

The complete root recipes in `examples/ai4s`, `examples/report-consistency`,
`examples/claim-evidence-review` and `examples/guarded-claim-review` each provide
`run.toml`. Their historical provider outcomes remain in their existing reports;
a new entrypoint does not improve those outcomes by itself. Skill-only resource
packages are not standalone workloads. Method packages retain explicit import,
instantiation, test and assessment commands before ordinary task execution.
