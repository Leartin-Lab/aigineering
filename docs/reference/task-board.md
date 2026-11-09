# Task board and operations

Normal CLI operations require Redis. SQLite remains the durable fact and claim
authority. Start the repository service or configure `AIGINEERING_REDIS_URL`:

```bash
docker compose up -d redis
aig redis status --json
```

The default URL is `redis://127.0.0.1:6379/0`. `--help` works without Redis;
normal application queries do not silently fall back to SQLite.

## Create and run a task

Initialize a workspace and publish explicit inputs and outputs:

```bash
aig domain init
aig asset add --name source --content-file source.md
aig task create --name report --description "Write a report from source." \
  --input source --activation source --output report
aig task list --json
aig task board
aig task show <contract_id> --json
aig run --task <contract_id> --json
aig task status <contract_id> --json
```

Contracts freeze their input and output requirements. Output requirements may
select exact Definition, content, producer actor and shape. An admitted Asset
from any source can satisfy those conditions; a root identifier, origin label
or historical tree does not add a completion gate. An independent acceptance
policy still requires its exact qualification.

The board and status are read-only views of committed facts. They show
unfinished outputs, missing inputs, capability or claim blockers, and
independent acceptance separately. They do not schedule work or repair state.

## Inspect and export exact results

Use the exact Asset IDs shown by status, audit or `aig asset show`. `result`
exports only an output whose requirements are satisfied and whose independent
qualification is present when required:

```bash
aig task audit <contract_id> --json
aig task result <contract_id> --output report --to report.md --json
```

Without `--to`, the command lists result metadata. It does not overwrite an
existing file. Sealed, non-promptable, tombstoned or transformed content is not
exported. Same-name output sharing is allowed only when the declared conditions
match; use exact selectors when identity matters.

## Retry, cancel and diagnose

Cancellation is an explicit signed action. Retry creates new ordinary work; it
does not reset the old claim. A retry or recovery needs an actual available
causal grant. A root retry cannot mint funding. If no valid funded work can be
published, inspect the blocker and resolve the funding or source issue rather
than expecting a hidden scheduler to create a successor.

```bash
aig task cancel <contract_id> --reason "Inputs withdrawn" --json
aig task retry <contract_id> --json
aig task runs <contract_id> --json
aig task diagnostics <contract_id> --claim <claim_id> --json
aig trace --contract <contract_id> --json
```

Continue only the exact returned retry or recovery Contract ID. Do not rerun a
submitted producer because independent acceptance is pending. Invocation
failure is an attempt observation, not by itself a permanent business failure
of the Contract. Integrated failure/recovery behavior remains subject to
verification.

`--timeout` bounds one provider request. `--wait-timeout` bounds a target or
configured run. Provider retries within an invocation are distinct from
publishing new runtime work. Unknown usage remains unknown, not zero or a bill.

## Independent review

Review is ordinary task and Asset publication. `request-review` publishes a
reviewer Task and its descriptive request Asset in one Candidate. The request
Asset ID is the review request ID. The reviewer report is an ordinary admitted
Asset. The decision command publishes canonical `asset.attest` and a
descriptive decision Asset in one Candidate:

```bash
aig task request-review <target_contract_id> --output report \
  --asset <exact_output_asset_id> --reason "Check the stated result." --json
aig task reviews <target_contract_id> --json
aig run --task <reviewer_contract_id> --json
aig task review decide <request_asset_id> --report <report_asset_id> \
  --verdict accept --reason "Policy checks passed." --json
```

Use the reviewer Contract ID returned by `request-review`. Use a distinct authorized reviewer when required by policy. The frozen policy
selects the target output and report conditions; no hidden task-origin, claim or
tree rule replaces those conditions. The descriptive request/decision Assets
do not themselves qualify a business output. Only the canonical attestation
does. Acceptance records policy approval of the selected assertion; it is not
proof of universal truth.

## Read-only history and operation limits

Status, audit, lineage, diagnostics, traces and local logs inspect facts. They
do not publish completion, recovery, assignments or other control state. Some
older handoff, revision, assignment, dependency, hold, priority, comment and
attachment-link records may remain readable as history. They are not current
fresh writer workflows and do not govern present claim or output eligibility.

Operational claims and renewals carry signed Candidates and are rechecked
against the current key, capability, registration and claim state inside the
SQLite writer transaction. Redis synchronization failures preserve accepted
SQLite facts and report the pending relay. Replay the exact original intent
through the same owner after restoring Redis.

## Upgrade

The current schema 24 reader floor prevents older processes from opening an
upgraded Store. Stop Workers and back up before upgrading; the floor cannot
constrain a connection that an older process already opened. Use the explicit
upgrade and Redis rebuild commands for the workspace:

```bash
aig domain upgrade --json
aig redis rebuild --json
```

See [configured runs](configured-runs.md), [Harness integration](agent-harness-migration.md),
and the [support and limits](development-status.md) for execution settings,
signed integration and evidence scope.
