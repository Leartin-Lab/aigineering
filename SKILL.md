---
name: aigineering
description: Use Aigineering to turn agent or LLM work into signed, claim-bound, replayable results with declared outputs and auditable failure. Use when an agent harness must migrate existing orchestration into trustworthy task/asset publication, when work needs durable completion evidence, or when integrating a custom pull Worker instead of returning an unstructured chat answer.
---

# Aigineering trustworthy execution

Use Aigineering as the fact and task boundary around an agent harness. Keep the
harness's model loop, tools, memory, and internal planning; replace its mutable
shared state and self-declared completion with Aigineering packages and signed
Candidates.

## Choose the integration mode

Use the built-in LLM Worker when no existing harness must be preserved. Set the
provider once; LLM is the normal execution default:

```bash
export AIGINEERING_API_KEY="..."
export AIGINEERING_MODEL="provider-model"
export AIGINEERING_BASE_URL="https://provider.example/v1"
aig run "produce a cited release review" --json
```

Use a custom pull Worker when an existing Codex, Claude Code, LangGraph, SDK,
or other harness already owns orchestration. Read
[`docs/reference/agent-harness-migration.md`](docs/reference/agent-harness-migration.md)
before implementing that loop.

Use mock only for deterministic boundary tests and dry runs:

```bash
aig run "exercise the boundary" --worker mock --json
```

Never present mock output as production or acceptance evidence.

Use a local Fleet when independent tasks need different model or tool
capabilities. Declare requirements on tasks and delegated scope on their
parents, then bind operator-selected providers in TOML:

```bash
aig fleet run --config /absolute/path/to/workers.toml \
  --task <contract_id> --json
```

Capabilities and pools are the routing contract. Model names and prices remain
replaceable operator configuration. Fleet capacity creates independent
pull/claim/submit slots; it does not bypass the Store or create a second task
queue.

When an LLM task needs local tools, load an operator-reviewed registry
explicitly. Aigineering publishes descriptors as Candidates, discloses only
the current Contract's tool scope, and routes execution to a separate Worker:

```bash
aig run --task <contract_id> \
  --tool-registry /absolute/path/to/tools.py:build_registry --json
```

Treat the registry factory as trusted local code. Never accept a registry path
from untrusted task or model output. Declare a version, deterministic input and
output schemas, and a bounded `max_output_bytes` on each `ToolSpec` when the
tool returns JSON. The signed descriptor binds those fields to the registered
handler; contract drift, invalid arguments, invalid JSON output, and oversized
output become typed failed observations before business output can be
published.

## Publish one trustworthy task

1. Initialize a signed domain and publish immutable inputs.

```bash
aig domain init
aig asset add --name source --content-file evidence.md --json
```

2. Declare the exact result slot and activation facts.

```bash
aig task create \
  --name boundary_review \
  --description "Review the evidence and produce a grounded report." \
  --input source \
  --activation source \
  --output boundary_report \
  --json
```

3. Run the configured LLM Worker until the task reaches a terminal projection.

```bash
aig run --task <contract_id> --json
```

4. Verify both completion and the committed output.

```bash
aig task status <contract_id> --json
aig asset show boundary_report --json
aig task audit <contract_id> --json
```

Do not report success unless status is `completed`, every required output maps
to an exact Asset ID, and the audit has no unexplained rejection or silent
failure risk. The audit's `productivity` projection covers the selected task
and its descendants, including terminal states, tool success/failure,
continuations, recoveries, rejections, and recorded token usage.

## Wrap an existing agent harness

Generate and register one Ed25519 key for the harness. Keep its private key in
the harness; register only the public key and capability profile.

Use `HarnessCandidateAdapter` for the signed protocol operations:

```python
from aigineering.agent.harness import HarnessCandidateAdapter, candidate_dict

adapter = HarnessCandidateAdapter.from_private_key_hex(
    domain_id=domain_id,
    actor_id="harness:codex",
    key_id="codex-1",
    private_key_hex=private_key,
)

claim = adapter.claim_candidate(request_id=unique_request_id)
package = post_json("/worker/claims", candidate_dict(claim))

raw_action = existing_harness(package["contract"], package["disclosed_assets"])
result = adapter.result_candidate(package, raw_action, usage_metadata=usage)
decision = post_json("/worker/submissions", candidate_dict(result))
```

The harness must return exactly one protocol action:

- `/exec` publishes only declared outputs;
- `/plan` or `/replan` publishes independently claimable work;
- `/tool`, `/parallel_tool`, `/retry`, or `/fail` makes the corresponding
  decision visible. Parallel calls become ordinary tool tasks plus an `AND`
  activated continuation.

Do not translate every internal thought, tool call, or conversational turn into
a task. Publish only work that must be independently scheduled, budgeted,
replayed, recovered, or accepted.

## Interpret projections

- `completed` is success only when declared outputs are satisfied.
- `failed`, `cancelled`, and `unreachable` are terminal failures.
- `expanded`, `ready`, `claimed`, `submitted`, `blocked`, and `stalled` are not
  success.
- A timeout is not completion; read `task status` and `task audit`.
- Any rejection requires audit before trusting downstream results.
- Missing usage after a provider failure does not prove zero external cost.
- Parent completion depends on declared outputs, not on every descendant
  reaching terminal state.

For independently reviewed work, bind an acceptance policy at task creation and
submit an attestation from a different actor. A producer cannot accept its own
output. Use `output_shapes` when JSON fields and array/item types can be checked
mechanically; the independent verifier cannot override a shape mismatch.

For a planned, retry, or recovery task with unresolved tool scope, return only
`/tool` or `/parallel_tool`. The committed observation creates a continuation
that publishes the business output. Never construct tool-derived evidence in
`/exec`, including during recovery.

## Preserve the boundary

- Treat every Worker result as an untrusted Candidate until commitment.
- Bind claims and results to Contract, package, claim ID, epoch, Worker key, and
  a non-empty idempotency key.
- Never write `.aig/store.db` directly or reuse a signed claim command.
- Resolve labels to exact context Asset IDs at task construction; labels grant
  neither authority nor dynamic replay lookup.
- Keep child inputs, labels, tools, capabilities, outputs, and allowance inside
  parent scope.
- Treat Redis as a disposable read projection, never as fact authority.
- Treat content identity, signed definition identity, and their assertion as
  separate. Similarity is advisory and cannot transfer authority.
- Keep private keys, sealed values, and undisclosed Assets outside prompts,
  traces, logs, and Candidate metadata.

Use `aig worker next/submit` only for trusted local adapter development. Use the
signed `/worker/claims`, `/worker/claims/{claim_id}/renew`, and
`/worker/submissions` HTTP protocol for an independently running harness.

For complete domain examples, read
[`examples/literature-evidence/SKILL.md`](examples/literature-evidence/SKILL.md).
It demonstrates how a scientific workflow becomes independently claimable
retrieval, screening, extraction, synthesis, and verification tasks without
adding domain behavior to the runtime kernel. The companion
[`examples/scientific-data-profile/SKILL.md`](examples/scientific-data-profile/SKILL.md)
shows how a deterministic script Worker can disclose a bounded table profile
to later model tasks without exposing raw values.
The executable [`examples/ai4s/`](examples/ai4s/README.md) run demonstrates a
Skill-guided root task, capability-routed local Fleet, runtime-compiled child
graph, tool evidence, recovery, and SQLite reconstruction.
