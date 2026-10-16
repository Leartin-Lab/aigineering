# Agent harness migration

This reference explains how an existing agent harness becomes an Aigineering
Worker without replacing its internal model, tools, memory, or planning loop.
The normative runtime boundary remains in [`DESIGN.md`](../../DESIGN.md) and
[`boundary-invariants.md`](../boundary-invariants.md).

## Migration model

| Existing harness concept | Aigineering boundary |
| --- | --- |
| user request or durable evidence | immutable input Asset |
| independently schedulable obligation | Contract |
| model/tool context for one invocation | WorkerPackage disclosure |
| final answer | `/exec` Candidate over declared outputs |
| delegated work | `/plan` or `/replan` Candidate |
| retryable attempt | `/retry` Candidate |
| unavailable evidence | `/fail` Candidate |
| harness run log | optional local diagnostic, never runtime truth |
| accepted shared result | committed Asset plus terminal projection |

Do not translate internal chain-of-thought, every tool call, or every harness
node into a Contract. A task boundary is useful when work must be independently
claimed, budgeted, recovered, replayed, routed, or accepted.

## One-time registration

Create an Ed25519 key inside the harness secret store. Register its public key,
stable actor ID, key ID, capability labels, pool, and version through
`aig worker register` or an equivalent signed control-plane Candidate. Never
send the private key to Aigineering.

The Worker registration drives eligibility. Capabilities describe what the
harness can execute—such as `vision`, `repository.write`, or
`compliance.review`—rather than price, persona, or prompt style.

`aig worker registrations WORKER_ID --json` displays the current registration,
full immutable history and promotion records. For canonical local identities,
an initial pull-only registration may advance once to a concrete adapter profile
under the same Worker ID and key; active claims or other metadata changes block
this automatic path. Explicit external registrations keep their existing
semantics. Local hosts bind the selected version through the claim writer fence.
Profile describes compatibility and does not prove which code executed. Stop
older local Worker processes before promotion. See
[ADR-039](../adr/ADR-039-local-worker-profile-promotion.md).

## Pull and submit loop

Use `aigineering.agent.harness.HarnessCandidateAdapter` to avoid reimplementing
canonical JSON, package binding, graph output, recursive task compilation, or
Ed25519 signatures.

1. Create a unique signed `worker.claim` Candidate. This is the request itself,
   not a Worker ID assertion; keep the Candidate intact through the request.
2. POST it to `/worker/claims` and retain the returned WorkerPackage unchanged.
3. Give the harness only `contract`, `disclosed_assets`,
   `method_context_assets`, `tool_scope`, and the visible remaining allowance.
4. Convert the harness outcome into exactly one Aigineering action string.
5. Compile that raw action text with `result_candidate(package, raw_action)`.
6. Serialize the returned signed Candidate for the submission transport.
7. Read task status and audit; never infer commitment from an HTTP send alone.

For a normal `/exec`, build the action string with the declared output names
and non-empty text values. This copyable single-output example assumes the
package Contract declares an output named `report`:

```python
import json
import subprocess

from aigineering.agent.harness import candidate_json

raw_action = "/exec " + json.dumps(
    {"outputs": {"report": "A concise report based on the package"}}
)
result = adapter.result_candidate(package, raw_action)
signed_candidate_json = candidate_json(result)
subprocess.run(
    ["aig", "worker", "submit", "--json", signed_candidate_json], check=True
)
```

This action/sign/submit fragment assumes `adapter` and a claimed `package`
are already available from the preceding loop. For an HTTP client whose
`json=` parameter serializes a Python object, pass `candidate_dict(result)`.
Use `candidate_json(result)` for the CLI's JSON-text option or an HTTP raw
JSON body; do not pass that string to a client's `json=` parameter, which
would encode the JSON string a second time.

For multiple outputs, the `outputs` mapping must contain every exact declared
output name and no undeclared names; each value must be non-empty text. The
nested `outputs` mapping is the recommended canonical example. The existing
action parser also supports a direct output-name mapping, such as
`/exec {"report":"A concise report"}`. In either form, the `/exec` prefix
and JSON body are part of the raw action string; a JSON object keyed by
`/exec` is not action syntax. `candidate_json()` serializes the signed
Candidate produced from that raw action, and that signed Candidate JSON is
what the submission endpoint consumes.

One claimed package produces one signed action. If the work needs another
invocation, publish a bounded ordinary child task through that action; keep the
harness's internal planning, scratch state and tool loop private to the current
invocation. Do not treat a hand-written domain DAG or harness scheduler as
runtime task state.

For a tool-bearing action, `/tool` and `/parallel_tool` follow the runtime's
declared tool scope and continuation grammar; they do not publish business
outputs directly. The repository [Skill](../../SKILL.md) describes the supported
action sequence and failure boundaries. Do not assume arbitrary parallel calls,
automatic recovery, or complete tool-continuation lineage from the action name
alone.

Current failure handling has two limits. A failed single `/tool` observation
terminalizes its parent as `failed`; `retryable` is metadata and starts no new
attempt. Inspect `task status` and `task audit`; to retry, publish `task retry ID`.
A configured local Worker can run the returned successor with
`aig run --task RETRY_ID`. An external Worker signs a new claim Candidate targeting
`RETRY_ID`, then resumes the same claim/submit loop below. A
`/parallel_tool` join's `AND` activation only checks that observation Assets
exist, so an `ok: false` observation may make the join eligible. Inspect each
exact observation before continuing; if the join is still unclaimed, hold it
with `task block JOIN_ID --reason TEXT`. These paths do not provide unified
automatic failure or recovery.

All operational entry points use the same signed coordination boundary. SQLite
rechecks the current enabled Worker registration, actor/key authorization,
`worker.submit` capability and requested claim target while committing the
claim. A successful claim Candidate is single-use. A null-target request that
finds no eligible work commits no request fact, so it may be retried later; once
it returns a package, create a new Candidate for another claim. Claim grant
facts link to the request record, which links to the Candidate receipt.

For work longer than the lease, sign a fresh renewal Candidate with
`renewal_candidate(package, request_id=...)` and POST it to
`/worker/claims/{claim_id}/renew`. The renewal carries the original claim ID and
epoch and rechecks the current enabled registration and actor/key binding inside
the writer transaction. A successful request ID is single-use; replay does not
extend the lease again. Each renewal fact links to both its original grant and
renewal request, and the request links to its Candidate receipt. Result
idempotency is deterministic for the exact package so transport replay is safe.

For a locally invoked `aig worker next`, an already registered Worker must use
its matching local signer or pass an inline signed Candidate with
`--candidate JSON`. The option takes JSON text, not a file path. The CLI will not
create or rebind a private key for an external registration. Omitted target and
lease options use the signed Candidate's values; explicit options must match.
For example, a harness that already compiled and signed each command can use
the same Candidate JSON with `aig worker next --candidate "$CLAIM_JSON" --json`
and submit its signed result using `aig worker submit --json "$RESULT_JSON"`.

## Operator CLI loop and machine results

An agent operating through the CLI can use the same one-package loop:

The example initializes a new workspace and publishes its input. Redis must be
reachable, and the registered Worker must meet the task's capability/pool needs.

```bash
aig domain init  # once per new workspace
aig asset add --name source --content-file source.md --json
aig task create --name report --input source --activation source --output report --json
```

For a local Worker with its matching signer, claim with:

```bash
aig worker next --worker-id WORKER_ID --json
```

An external Worker passes its signed claim Candidate as JSON text instead:

```bash
aig worker next --candidate "$CLAIM_JSON" --json
```

Build `CLAIM_JSON` with `HarnessCandidateAdapter.claim_candidate()` and
`candidate_json()` from `aigineering.agent.harness`. Compile one action for the
package using `result_candidate(package, raw_action)`, then use `candidate_json()`
for `SIGNED_CANDIDATE`; this invokes the canonical Worker action compiler.

```bash
aig worker submit --json "$SIGNED_CANDIDATE"
aig task status CONTRACT_ID --json
aig task result CONTRACT_ID --output report --to report.md --json
```

The local-signer and external-Candidate `worker next` forms are alternatives;
do not combine them. `result_candidate()` uses the canonical Worker action
compiler and binds the result to the exact claimed package.

The exact signing and claim binding rules above still apply. A successful
submission only commits accepted effects; check projected status and the
Contract's acceptance policy before exporting. `worker package` is an operator
metadata preview (`submittable: false`), not a claim or a WorkerPackage.
`mcp add` publishes a capability descriptor Asset; it does not connect to an
MCP server, provide transport, or execute a tool.

The CLI keeps command-specific JSON and exit behavior; there is no universal
result envelope. These fields describe current commands, not a stable shared
schema:

| Command | Successful JSON | Non-success boundary |
| --- | --- | --- |
| `task create --json` | `ok`, `contract_id`, `name`, `outputs` | Click usage or publication errors can use Click's command error output. |
| `task status ID --json` | projected task fields such as `status`, `blockers`, `outputs`, and `integrity` | Missing task or inconsistent integrity exits nonzero; inspect the emitted error or integrity details. |
| `task result ID --json` | `ok`, `data` (`contract_id`, `status`, `outputs`, `written_path`), `revision` | Incomplete or unqualified results return an error envelope and nonzero exit; invalid output selection may exit 2. |
| `worker next --json` | A claimed package object | No eligible task can return `null`, a capability-blocked object, or a policy-blocked object. Errors exit nonzero. |
| `worker submit --json` | Submission result from the shared committer | Rejection or synchronization errors exit nonzero. A synchronization error can follow commit, and current output may not retain accepted IDs; restore Redis and inspect task status/audit before deciding whether to replay the exact Candidate. |
| `worker package --json` | `view: operator_package_preview`, `submittable: false`, metadata | Missing task exits nonzero. It never returns package identity or asset bodies. |

Use `task show`, `task audit`, and `task lineage` when status alone does not
explain a blocker or successor. `task lineage` currently verifies supported
containment, retry, recovery-from-proven-invocation-failure, handoff, and
revision relations; it does not pair a particular failure with a recovery
publication or include every tool-continuation relation. For the documented
read-only views, see the [task operations reference](task-board.md).

## Preserve existing orchestration

An existing harness may continue to choose models, run tools, compact its local
context, or maintain a private scratchpad. Those mechanisms remain inside one
Worker invocation. They become shared truth only through a result Candidate.

When the harness decides that work should survive the current invocation, it
must publish ordinary child tasks:

- `/plan` for missing information or decomposable work;
- `/replan` after a prior assumption or route becomes invalid;
- `/tool` only for a tool declared in the package;
- `/retry` only when the same task can be safely attempted again;
- `/fail` when completion would require fabricated evidence.

This preserves the harness's productive logic while moving scheduling,
authority, allowance, commitment, reconstruction, and acceptance to the common
runtime.

## Completion contract

A successful transport response is not enough. Before returning a trustworthy
result to the harness user, require all of the following:

- projected task status is `completed`;
- each declared output resolves to one exact committed Asset ID;
- rejection and silent-failure-risk lists are empty or explicitly adjudicated;
- independent acceptance is satisfied when the Contract requires it;
- a fresh process can read the same status and output from the authoritative
  Store.

On `failed`, `cancelled`, `unreachable`, `stalled`, or timeout, return the
projected failure and audit evidence. Do not silently fall back to the harness's
uncommitted answer.

## Security and deployment

- Keep Worker private keys and provider credentials in the harness secret
  boundary.
- Authenticate and encrypt HTTP transport at deployment level; the reference
  server is not a hostile-network security profile.
- Never log undisclosed package content or raw private reasoning.
- Treat package IDs, claim epochs, and idempotency keys as protocol values, not
  mutable harness metadata.
- Treat direct recordless Store claim calls as trusted internal compatibility
  seams, not as authenticated operational requests or deployment evidence.
- Do not let a producer issue its own independent acceptance attestation.

See [ADR-037](../adr/ADR-037-transactional-worker-authentication.md) for the
transactional authentication boundary and [ADR-012](../adr/ADR-012-authenticated-worker-coordination.md)
for its original HTTP scope.
