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

## Pull and submit loop

Use `aigineering.agent.harness.HarnessCandidateAdapter` to avoid reimplementing
canonical JSON, package binding, graph output, recursive task compilation, or
Ed25519 signatures.

1. Create a unique signed `worker.claim` Candidate.
2. POST it to `/worker/claims` and retain the returned WorkerPackage unchanged.
3. Give the harness only `contract`, `disclosed_assets`,
   `method_context_assets`, `tool_scope`, and the visible remaining allowance.
4. Convert the harness outcome into exactly one Aigineering action.
5. Compile the action with `result_candidate(package, raw_action)`.
6. POST the signed Candidate to `/worker/submissions`.
7. Read task status and audit; never infer commitment from an HTTP send alone.

For work longer than the lease, sign a fresh renewal Candidate with
`renewal_candidate(package, request_id=...)` and POST it to
`/worker/claims/{claim_id}/renew`. A request ID is single-use. Result
idempotency is deterministic for the exact package so transport replay is safe.

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
- Do not let a producer issue its own independent acceptance attestation.
