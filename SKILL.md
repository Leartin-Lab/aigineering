---
name: aigineering
description: Use Aigineering to run signed, bounded tasks with exact Asset evidence, policy acceptance and observable blockers.
---

# Aigineering agent workflow

Use Aigineering as a durable boundary around agent work. A committed output is
an authorized assertion; it is not automatically true or independently
accepted.

## 1. Declare inputs and authority

- Use a selected workspace and its signed domain. Redis is required for normal
  discovery, event feeds and Worker wakeups; SQLite stores signed facts and
  claim state.
- Publish or select exact input Assets. Freeze input roles, output
  requirements, capabilities and a real causal budget in the Contract.
- Give child work only scope and funding available from its parent.
- Skills and labels select context; they do not grant capability or authority.
- MCP descriptors describe capability. They do not connect a transport or
  provide a tool executable.
- Keep credentials and undisclosed values out of prompts and task metadata.

## 2. Find and inspect ordinary tasks

Planning and delegation publish ordinary bounded Contracts. A tool scope
permits use but does not force a call. A single-tool action declares its tool
task and continuation in the same signed Candidate. Tool observations alone
cannot satisfy business outputs.

Use the task board to find work, then inspect the exact task and inputs before
acting:

```bash
aig task list --json
aig task status TASK_ID --json
aig task context TASK_ID --json
```

Context preview is metadata-only by default. Select an exact Asset and opt into
content only when needed. See [task operations](docs/reference/task-board.md).

## 3. Execute and submit signed work

Use a configured Worker or Harness through the normal claim flow. A Harness
must submit the original signed claim provenance through
`HarnessCandidateAdapter`. Never submit raw model text as if it were a signed
Candidate. `aig worker next` acquires a claim; it does not run a model or
produce output.

Use `/exec` for declared output publication, `/tool` or `/parallel_tool` for
authorized tool work, and `/plan` for bounded child work. The runtime checks
the current signing key, capability, registration, claim, scope and funding in
the writer transaction. Do not edit the Store or invent IDs, signatures,
observations or evidence. Mock Workers are for tests and dry runs only; never
present mock output as production.

## 4. Check output requirements

Inspect status, audit and exact Asset records after a publication:

```bash
aig task status TASK_ID --json
aig task audit TASK_ID --json
aig asset show --id ASSET_ID --json
```

An admitted Asset from any source can satisfy the Contract's frozen name,
Definition, content, producer and shape conditions. Historical origin or tree
metadata does not add another completion gate. Independent acceptance still
requires its exact canonical qualification. A successful Candidate submission
does not by itself establish completion, qualification or semantic truth.

Do not rerun a submitted producer just because independent acceptance is
pending. Export only the exact output Asset selected by status and policy:

```bash
aig task result TASK_ID --output OUTPUT_NAME --to PATH
```

## 5. Request policy review when required

Review request creates an ordinary bounded reviewer Task and descriptive
request Asset. A decision publishes the canonical attestation and a
descriptive decision Asset. Use the exact request and report Asset IDs:

```bash
aig task request-review TARGET_ID --output OUTPUT_NAME \
  --asset OUTPUT_ASSET_ID --reason "Check the result."
aig task reviews TARGET_ID --json
aig task review decide REQUEST_ASSET_ID --report REPORT_ASSET_ID \
  --verdict accept --reason "Policy checks passed."
```

Use a distinct authorized reviewer when the frozen policy requires it. A
report or relation Asset is not itself a qualification fact; only canonical
`asset.attest` produces qualification. Policy acceptance is not universal
truth.

## 6. Diagnose blockers and continue with funded work

On a rejection, timeout or blocker, keep the exact task and claim IDs and
inspect the committed facts:

```bash
aig task status TASK_ID --json
aig task runs TASK_ID --json
aig task diagnostics TASK_ID --claim CLAIM_ID --json
aig task audit TASK_ID --json
```

An invocation failure is an attempt observation, not by itself a permanent
business failure. Retry and recovery publish new ordinary work only when an
actual causal grant is available. They do not reset claims or mint a root
grant. If no funded work can be published, report the blocker and the evidence
needed to resolve it; do not describe a proposed or rejected recovery as
scheduled.

Redis synchronization failures preserve any accepted SQLite facts. Restore
Redis and replay the exact original intent through the same command owner when
the result says synchronization is pending. Provider timeout, run timeout,
retry count, causal allowance and usage are separate; unknown usage is not
zero.

## Further references

- [Task operations](docs/reference/task-board.md): create, run, inspect, retry,
  review and export.
- [Harness migration](docs/reference/agent-harness-migration.md): signed
  claim and submission protocol.
- [Configured runs](docs/reference/configured-runs.md): explicit Worker and
  Fleet profiles.
- [Support and limits](docs/reference/development-status.md): current scope
  and evidence limits.
