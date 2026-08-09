---
name: aigineering
description: Use the Aigineering 0.5 CLI and signed Worker protocol to create, run, inspect, and audit asset-driven tasks with declared outputs, recoverable delegation, and Candidate-to-Fact commitment. Use when work needs durable outputs, task status, replayable evidence, or a custom Worker integration rather than an unstructured chat result.
---

# Aigineering CLI Gateway

Use this skill when a task needs an auditable, recoverable execution result
instead of an unstructured chat answer. Aigineering manages tasks, assets,
candidate outputs, trace records, and declared-output commitment boundaries.

## When To Use

Use Aigineering for:

- ADR or boundary reviews that should return a durable report.
- Long-running planning, replanning, or recovery tasks.
- Release readiness checks that need traceable inputs and outputs.
- Any task where the result should be a committed asset with an audit trail.

Do not use Aigineering as a shell sandbox. Worker sandboxing is a worker
execution policy; Aigineering's boundary is candidate-to-fact commitment.

## Core Flow

1. Add input assets.

```bash
aig asset add --name adr_005 --content-file docs/adr/ADR-005-unified-feature-ingress.md --json
```

2. Create a task with declared outputs.

```bash
aig task create \
  --name boundary_review \
  --description "Review the implementation against ADR-005." \
  --input adr_005 \
  --output boundary_report \
  --label review \
  --json
```

3. Run a CLI worker until the task reaches a terminal status.

```bash
aig run --task <contract_id> --worker llm --model <model> --json
```

For deterministic local checks, use mock explicitly:

```bash
aig run --once --worker mock --json
```

For work that must be accepted by a different actor, bind the policy when the
task is created and attest the exact produced Asset afterward:

```bash
aig task create \
  --name compliance_review \
  --output compliance_report \
  --acceptance-policy '{"mode":"independent","policy_version":"review-v1","required_attestations":1,"verifier_capabilities":["verify.human"]}' \
  --json
aig verify attest \
  --contract <contract_id> \
  --output compliance_report \
  --asset <asset_id> \
  --json
```

The producer cannot attest its own output. The target must be the Asset created
by the task's claim-bound Worker submission, not an unrelated same-name Asset.
The policy version, rubric and evidence references are identity-bearing;
`verify attest` rejects evidence arguments that do not exactly match the task.

4. Read the committed output asset.

```bash
aig asset show boundary_report --json
```

5. Read the audit projection.

```bash
aig task audit <contract_id> --json
```

## Command Contract

Prefer these commands:

- `aig asset add/show/ls --json`
- `aig task create/status/wait/audit --json`
- `aig run --once --worker <mock|llm> --json`
- `aig run --task <contract_id> --worker <mock|llm> --json`
- `aig worker next/submit --json` only when implementing a custom worker loop.
- `aig cache status/rebuild --json` when the optional Redis read projection is
  configured.
- `aig graph contents/definitions/assertions --json` to inspect the immutable
  definition-content graph.

Avoid these commands for normal agent delegation:

- `aig demo`: quickstart only.
- `aig contract run`: deprecated direct execution entry.
- Direct store writes or scripts that mutate `.aig/store.db`.

## Interpretation Rules

- Treat `status: completed` with declared outputs as the normal success case.
- Treat `failed`, `cancelled`, and `unreachable` as terminal failures.
- Treat `blocked`, `blocked_delegation`, `blocked_capability`, `ready`,
  `claimed`, `stalled`, and `submitted` as non-success states.
- Do not claim a task is complete unless `task status` or `run --task` reports
  terminal completion and the expected output asset exists.
- If `rejection_count` is nonzero, read `task audit` before trusting the result.
- If `run --task` times out, report the timeout and current projected status.
- `token_usage` is claim-bound audit evidence, not billing authority. Missing
  usage on a provider failure does not prove that no external tokens were used.
- Treat `invalid_activation` and `descendant_invalid_activation` as malformed
  task definitions requiring repair, not as ordinary waiting.

## Boundary Rules

- Worker output is a candidate, not a fact.
- Only declared task outputs can become committed facts.
- Labels select context/asset injection; labels do not grant business authority.
- A claimed/submitted task must not be returned to an unclaimed state. Recovery
  or retry must create a new task.
- `/plan` and `/replan` publish independently claimable draft, dependency, and
  compile tasks. Treat an `expanded` root as unfinished, not successful or
  waiting on an in-process call stack.
- Planned children need executable descriptions and non-empty outputs; together
  they must cover the parent outputs and remain inside visible causal allowance.
- A planned dependency is valid only when its fact already exists or an accepted,
  reachable sibling produces it. Self-output activation, ungrounded cycles, and
  outputs promised only by rejected siblings are invalid plans, not waiting work.
- Parent task completion is based on declared output satisfaction, not on all
  child tasks finishing.
- An independent-acceptance task is incomplete until `output.qualified` binds
  its declared slot to one exact task-produced Asset ID.
- Remote claim and lease-renew requests are signed `worker.claim` and
  `worker.claim.renew` Candidates. Never send a self-reported `worker_id` body
  or reuse one command Candidate. Hosted `/exec` signs one atomic content,
  definition, and assertion effect batch; staged plan/replan and
  tool/fail/retry sign contained `contract.declare` effects. All bind the
  returned Contract/claim/package/epoch and a non-empty idempotency key. Custom
  Workers must compile raw actions before signing; `worker.output` and
  `task.delegate` wrappers are rejected.
- Custom Candidate clients must follow `conformance/README.md` and the versioned
  public vectors. Signed effect payloads and metadata do not allow floats,
  unsafe integers, non-string object keys, sets, bytes, NaN, or infinities;
  encode exact decimal values as strings.
- Human-assisted completion still requires a registered human actor key and a
  live claim; do not inject a reviewer decision directly into the Store.
- Redis is a disposable read projection. Set `AIGINEERING_REDIS_URL` only after
  installing `aigineering[redis]`; a cache outage must not be interpreted as
  task or fact loss.
- Content IDs hash normalized content only. Definition identity binds source and
  signer authority; an accepted signed assertion links the two. Do not infer
  authority from a shared content ID or a semantic similarity score.
- Current v4 tasks bind label-selected context to exact Asset IDs. Labels remain
  audit metadata after commitment and must not be resolved again during replay.
