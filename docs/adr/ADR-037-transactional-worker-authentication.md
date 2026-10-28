# ADR-037: Authenticate operational Worker coordination transactionally

Status: Accepted
Date: 2026-10-05
Supersedes: ADR-012 for operational claim and renewal paths

## Context

Worker result submission already carries a signed Candidate bound to the Worker
and claim. Claim and lease renewal also need to prove the Worker's current
identity: an unsigned Worker ID, a stale registration lookup, or a preflight
check alone must not reserve or extend a claim. This applies to local Fleet and
Engine paths as well as HTTP; moving a call inside one process does not make its
identity recheck atomic with the claim write.

## Decision

Every operational claim and renewal carries its original signed Candidate from
preflight through the SQLite writer transaction. The command parser accepts
only these exact payload shapes:

- `worker.claim`: `worker_id`, nullable `contract_id`, and positive integer
  `lease_seconds`;
- `worker.claim.renew`: `worker_id`, `claim_id`, positive integer
  `claim_epoch`, and positive integer `lease_seconds`.

The Candidate's signed idempotency key is also required. Unknown fields,
boolean-as-integer values, and implicit string coercion are rejected. At the
writer boundary, SQLite rechecks the Candidate signature against currently
effective actor keys, the `worker.submit` capability, the enabled Worker
registration and actor/key binding, the requested target or claim epoch, and,
for claim selection, the registration version selected during package
preparation. Renewal rechecks the current enabled registration and actor/key
binding; its signed payload has no separate registration-version field. The
existing receipt validation rejects revoked keys, while the current registration
check also rejects a non-revoking switch to another registered key after
preflight. The validated request records and claim transition commit atomically.
A race that changes registration or authorization after preflight rejects the
command without a partial claim or renewal. When an operational caller supplies
preflight request records, the writer compares record IDs and effective content,
ignoring only `recorded_at` timestamps.

All production claim paths use this protocol: HTTP, local WorkerHost, Fleet,
Engine, methods, CLI, and harness adapters. `WorkerHost` and
`HarnessCandidateAdapter` share the canonical coordination Candidate compiler.
The HTTP surface receives and verifies the original Candidate. A successful
signed claim or renewal request is single-use: replay does not create another
claim or extend the lease again. A signed null-target scan that finds no work
writes no request fact, so the same Candidate can be retried when work later
becomes eligible.

Committed coordination provenance is explicit. A signed `claim.granted` fact
causally references its canonical `worker.claim.requested` record. A signed
`claim.renewed` fact references both the original grant and its
`worker.claim.renew.requested` record. Those request records in turn reference
the Candidate receipt. Recordless trusted Store compatibility calls retain
their legacy fact shape; they are internal/library seams and are not an
operational authentication path.

`aig worker next --candidate` accepts inline Candidate JSON. If an existing
Worker registration is not backed by the canonical local signer, `worker next`
requires a matching signed Candidate; it never guesses, creates, or rebinds a
foreign Worker's private identity. With no existing registration, the local
pull-only path may provision its ordinary local identity. Expected authorization
and writer-fence failures are returned as a JSON error for `--json` or a Click
error for text output.

The existing schema20 floor and fact types are unchanged. This decision does
not turn trusted low-level Store methods into remote APIs, revoke already-open
old connections, or provide process isolation for arbitrary local Python code.

## Consequences

The same Worker key proves intent to reserve, renew, and submit work across all
operational entry points. Store arbitration remains authoritative; Worker
selection and any preview are not authority. A no-work scan is retryable because
there is no durable request to consume, while a successful mutation has one
canonical Candidate identity and one durable outcome.

The CLI can use an existing local signer or accept an explicitly supplied
signed Candidate. It does not mint credentials for an already registered
external identity. Harnesses can use the common adapter rather than rebuilding
payloads, signatures, or canonical request records.

## Verification scope

Behavior tests exercise writer revalidation after registration/key changes,
single-use claim and renewal requests, no-work retry, causal provenance,
projection rebuild, local and HTTP paths, and the recordless compatibility
shape. These tests do not claim full-suite or release verification; see the
current development evidence for the exact tested snapshot.

## Related decisions

- [ADR-012: Authenticated worker coordination commands](ADR-012-authenticated-worker-coordination.md)
- [ADR-006: Worker substitutability](ADR-006-worker-substitutability.md)
