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
New root input roles select the exact Asset IDs from that resource publication,
using the existing `input_asset_bindings` guard, even when the preset omitted a
sensitive policy. Other sensitive-policy fields remain intact. Conflicting
explicit active bindings refuse root declaration; setup resources may already
be committed. Historical same-name inputs cannot join that selection. Resume
continues the existing Contract and does not update its bindings.
The result includes `contract_id`, projected `status`, `completed`, `timed_out`
and `worker_errors`. Completion satisfies the Contract policy; it does not prove
general semantic correctness. `--json` emits one result object, not a streaming
format shared with another harness.

New tasks use mechanical output conditions. With no explicit output selector,
the compatibility condition is a matching business Asset name, regardless of
producer Contract tree. To bind an output role to an existing semantic
Definition or exact version, configure `acceptance_policy.output_requirements`
with `definition_id`, optional `content_id`, and optional sorted
`producer_actor_ids`. A syntactically valid Definition may be required before it
is published; until its signed graph fact exists, the output remains unmet and
does not fall back to its name. The legacy mechanical `output_source` field is
readable but no longer adds a tree restriction. Independent acceptance still
requires exact qualification for its declared output slot.

The SQLite reader floor is schema23. Stop old Workers and run
`aig domain upgrade --json` before relying on the new output conditions; the
command backs up before migration. An older process with an already-open
connection is not fenced. Existing signed Engine bridge history is immutable;
if it does not satisfy a newly compiled goal Definition and mapped-input
conditions, continue with new ordinary work rather than rewriting that history.

Add `--once` to traverse the configured Worker slots until one eligible
package is claimed, invoke at most that one package, and return the current
projection at the cycle boundary. This supports ordinary task planning and
continuation between invocations; it does not imply that the root Contract is
complete. For an existing target, use `--resume TASK_ID --once` (or the `--task`
alias) with `aig run --config`, or `aig fleet run --config PATH --task TASK_ID
--once`. These bounded commands share the one-package limit. Without `--once`,
configured runs retain their existing run-until-terminal behavior. Incomplete
JSON results include the exact Contract projection, blockers, stop reason and a
state-aware next step. A submitted task blocked on independent acceptance
directs the operator to review; it does not suggest rerunning the task. A
resumable target includes an exact `--resume` command.

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

## Bring a Skill or tool to a technical trial

Use a fixed checkout for the first trial; example adapters must be importable
in addition to the installed runtime. Start from the
[CLI research recipe](../../examples/cli_research/README.md), reproduce it in a
fresh workspace, then change the explicitly selected inputs, Skill and adapter.
This is a supported trial path, not automatic compatibility with every workload.

A Skill directory needs `skill.toml` with `name`, `version` and a relative
`content_file` (default `skill.md`). List the directory under `run.skills` and
select the content Asset through the task labels, as the recipe does. A standalone
`SKILL.md` is not automatically loaded. Skill content supplies instructions;
Worker capabilities, tool scope, delegation bounds and acceptance remain explicit.

`aig mcp add` publishes capability metadata without connecting to a server.
Keep an existing Harness's MCP client behind its
[signed Worker boundary](agent-harness-migration.md), or provide an explicit
tool/Worker adapter. A configured tool registry selects an importable factory;
it does not discover arbitrary MCP transports. Staged `/plan` expansion uses
ordinary runtime tasks and must remain within the root's frozen scope.

## Delegate to an existing local workspace

Initialize two distinct workspaces with `aig -C PATH domain init`. Obtain the
receiving domain's Genesis ID with `aig -C PATH domain show --json`. Both
workspaces retain their own history; switching directories does not reset it.
The operator must control both domains and explicitly provide the receiver's
root key. This is a local trust arrangement, not a remote security boundary.

Add an Engine adapter to the source Fleet:

```toml
[fleet]
db_path = ".aig/store.db"

[[workers]]
id = "engine:analysis"
kind = "engine"
capabilities = ["analysis"]
target_workspace = "/absolute/path/to/receiver"
target_domain_id = "GENESIS_ID"
receiver_fleet = "/absolute/path/to/receiver-workers.toml"
receiver_key_file = ".aig/identity/root.ed25519"
```

Use the receiver's actual existing key filename. `target_workspace` is relative
to the source workspace; `receiver_key_file` and the receiver Fleet's `db_path`
are relative to the target workspace. The receiving database must be
`.aig/store.db`. `receiver_fleet` is relative to the source Fleet TOML directory.
Tool registry resources inside the receiver Fleet are relative to the receiver
Fleet TOML directory. The receiver Fleet declares ordinary
`llm`, `local` or `tool` Workers with the capabilities needed by the delegated
Contract. It cannot contain `engine` Workers. The source domain must already
exist before preparing this configuration.

Run the source through the existing `aig fleet run --config PATH --task ID`
or configured-run entrypoint. The bridge imports only disclosed inputs, binds
their receiving-domain IDs, and uses normal signed registration and task
execution. New operations have distinct inner roots; exact-claim replay reuses
the original operation. Changing receiver configuration cannot refresh that
claim's recorded intent. Use one frozen receiver configuration at a time: the
history check is not a transactional uniqueness fence against concurrent first
invocations with different configurations. Stop bridge processes before changing
that configuration. The configuration fingerprint covers Fleet contents and
declared tool descriptor metadata. Keep referenced Python factories, handlers
and their dependencies immutable for the operation; profile/version metadata
does not attest implementation bytes. Inner mechanical completion does not satisfy an outer
independent-acceptance policy. Unsupported protected outputs or policy
translations fail explicitly.

Invocation stays within the inner root tree; ordinary receiver-domain runtime
maintenance still runs. Stopping the source does not cancel all receiver work,
and registry archive does not stop either Fleet. The reverse direction requires
a separate explicit profile. There is no shared budget, recursive federation,
or external-effect exactly-once guarantee.

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

Setup commits inputs, Skills and the exact-bound Contract in one signed Candidate.
Rejected or interrupted setup leaves none of those effects. An identical setup
replays its original publication; changed resources or requirements produce a
distinct intent and Contract, even when the display name is unchanged.
Existing matching Assets can satisfy the declared requirements; a historical
output name alone neither rejects creation nor grants qualification. Express
required content, producer or independent policy through ordinary selectors.
Use `--resume` to select an existing Contract without reloading its source files,
or a fresh workspace for a separate fact domain. Worker execution and identity
registration are separate actions; failure after accepted setup preserves its
facts. Task audit and runtime records remain authoritative.

## Example coverage

The complete root recipes in `examples/ai4s`, `examples/report-consistency`,
`examples/claim-evidence-review` and `examples/guarded-claim-review` each provide
`run.toml`. Their historical provider outcomes remain in their existing reports;
a new entrypoint does not improve those outcomes by itself. Skill-only resource
packages are not standalone workloads. Method packages retain explicit import,
instantiation, test and assessment commands before ordinary task execution.

The [CLI measurement research example](../../examples/cli_research/README.md)
adds a small synthetic workload with two chained tools, explicit success-envelope
schemas and independent report qualification. Its default profiles use the real
provider; deterministic local Workers are test fixtures only.
