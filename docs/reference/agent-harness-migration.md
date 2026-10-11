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
4. Convert the harness outcome into exactly one Aigineering action.
5. Compile the action with `result_candidate(package, raw_action)`.
6. POST the signed Candidate to `/worker/submissions`.
7. Read task status and audit; never infer commitment from an HTTP send alone.

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
