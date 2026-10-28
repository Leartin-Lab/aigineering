---
name: aigineering
description: Use Aigineering to turn agent work into signed, claim-bound task results with declared outputs, exact Asset evidence, policy acceptance, and observable recovery.
---

# Aigineering agent workflow

Use Aigineering as the durable task and evidence boundary around an agent. Keep
the model loop and tools in the harness; publish only work that must be
independently claimed, budgeted, reviewed, or recovered. A committed output is
an authorized assertion. It is not automatically true or independently
accepted.

## 1. Establish explicit inputs and authority

- Use a selected workspace and its signed domain. Redis is required for normal
  discovery, event feeds, and Worker wakeups; SQLite remains the signed fact
  and claim authority. There is no operational offline fallback.
- Publish or select exact input Asset IDs. Declare input roles, output names,
  capability requirements, labels, and budget in the task definition. Give
  children only scope already allowed by their parent.
- A Skill supplies instructions and context. It does not grant capability or
  authority. Labels resolve to frozen Assets; they are not dynamic permissions.
- MCP descriptors contain capability metadata; they do not provide an MCP
  transport. An operator must configure the executable adapter/registry.
- Keep credentials and undisclosed values out of prompts and task metadata.
  Never infer missing evidence or fabricate tool observations.

For local routing, use `aig board list/show/switch` or pass `--board NAME`;
explicit `-C PATH` selects a workspace directly. See [named workspaces](docs/reference/task-board.md#named-local-workspaces).

## 2. Plan ordinary work

For an existing root obligation, request a plan with `/plan` (or `/replan` on
an eligible task). The planner publishes bounded ordinary child tasks through
the runtime. Review their exact inputs, outputs, capabilities, labels, and
budgets before execution. Planning does not create a second scheduler or grant
new authority. Tool use, continuation, retry, review, and recovery must also be
ordinary runtime tasks.

Use `aig task list/board/show` to discover work, then inspect `aig task status
ID --json` and `aig task context ID --json` before acting. Context preview is
metadata-only by default; select an exact listed Asset and opt into content
only when needed. Read the [task operations reference](docs/reference/task-board.md)
for command options and specialized workflows.

## 3. Claim, execute, sign, and submit

Use a configured Worker or harness through the normal claim flow. For a
Harness, use `HarnessCandidateAdapter`: claim the exact package, produce one
protocol action, create the signed result Candidate, and submit that Candidate
through the worker submission interface. Never submit raw model text as if it
were a signed Candidate. `aig worker next` only acquires/signs a claim; it does
not execute a model or produce task output.

The action command envelope is distinct from provider response JSON. CLI
`--json` controls CLI output formatting; it does not request JSON from the
provider. Provider wire formatting is configured separately (for example,
`--response-format json_object`). Use `/exec` only for declared outputs,
`/tool` or `/parallel_tool` for tool work, `/plan` for bounded child work, and
the supported lifecycle action for retry/failure. Tool observations are local
evidence and cannot by themselves satisfy business outputs.

`HarnessCandidateAdapter` is the supported harness signing bridge. Keep its
key private and supply original signed claim provenance on submit. Never edit
the Store directly or invent IDs, signatures, observations, or evidence. Mock
workers are for tests/dry-runs; never present mock output as production or
acceptance evidence. See [harness migration](docs/reference/agent-harness-migration.md).

## 4. Check status and exact Asset evidence

After submission, inspect `aig task status ID --json`, `aig task audit ID
--json`, and the exact output Assets with `aig asset show --id ASSET_ID
--json`. Match each required output to its committed Asset ID. Do not rely on
name-only lookup when version identity matters. A successful submission is
not the same as a completed task, and `completed` alone does not establish
independent acceptance or semantic truth.

Statuses such as `ready`, `claimed`, `submitted`, `expanded`, `blocked`, or
`stalled` are not success. Do not rerun a submitted or complete task. If a
submitted result awaits independent acceptance, use the exact review flow;
if changes are required, continue through the published revision successor.
See [review and revision](docs/reference/task-board.md#request-an-exact-independent-review).

## 5. Apply policy acceptance, then export

When the Contract requires independent acceptance, use a distinct authorized
reviewer and the frozen acceptance policy. Inspect the exact report and record
an operator decision through the canonical review command. A review report is
ordinary output, not qualification by itself. Export only the exact output
Asset that status and policy identify as qualified:

```bash
aig task result TASK_ID --output OUTPUT_NAME --to PATH
```

Do not substitute a later same-name Asset, reviewer recommendation, or
unverified file for the bound evidence. Policy acceptance means the configured
criteria accepted that assertion; it does not prove universal truth.

## 6. Diagnose failure and continue with new work

On a nonzero command, timeout, rejection, or blocker, preserve the exact task
ID and inspect status, audit, runs, and (when needed) diagnostics:

```bash
aig task status TASK_ID --json
aig task audit TASK_ID --json
aig task runs TASK_ID --json
```

Use `aig task diagnostics TASK_ID --claim CLAIM_ID --json` with the exact claim
from `task runs`; use `task logs` only for available local invocation
observations. These views are diagnostic evidence, not authority to retry. Fix
the reported service/configuration issue, restore Redis and synchronize when
needed, then follow the exact `next_step` or published retry/recovery successor.
Recovery is new ordinary work: run its returned task ID. Never reset a claim,
rerun a submitted/complete producer, or describe a scheduled recovery as
completed. If necessary evidence is absent, report the blocker or fail with a
concrete reason; do not guess or fill the gap with generated evidence.

Accepted `redis_sync_pending` means SQLite retained the signed operation.
Keep its exact IDs/key and original arguments, restore Redis, and replay only
through the same owner as directed. Inspect partial bulk results individually;
an overall nonzero exit does not erase accepted members. See the [operations
reference](docs/reference/task-board.md) for exact replay and bulk recovery.

## Further references

- [Task operations](docs/reference/task-board.md): lifecycle, assignment,
  review, retry, recovery, diagnostics, and exact command forms.
- [Harness migration](docs/reference/agent-harness-migration.md): signed
  claim/submission protocol and adapter example.
- [Configured runs](docs/reference/configured-runs.md): explicit Worker/Fleet
  profiles and resumable configured execution.
- [Task attachments](docs/reference/task-attachments.md): signed attachment
  links and verified-byte export.
