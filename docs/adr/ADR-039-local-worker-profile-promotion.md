# ADR-039: Preserve local Worker identity across entry points

Status: Accepted
Date: 2026-10-05

## Context

An empty-domain `worker next` creates a local pull-only registration with an
unset profile. A later concrete execution adapter uses the same Worker ID and
key, but supplies a profile. Reusing registration version1 with different
metadata rejects before model invocation. An already constructed local host
also needs to retain the registration version it selected, rather than silently
claim through a later version.

## Decision

The shared local registration service may perform one bounded profile
promotion. The source must be the sole historical registration, currently
enabled, with default routing metadata, a positive canonical numeric version,
the canonical local actor/key and an effective unrevoked Worker signing key.
The adapter may change only an empty profile to a nonempty profile and advance
the version by one. An active claim blocks promotion, including an elapsed
lease whose claim row remains active. Existing local identities are loaded;
unsupported changes do not create a replacement key or rebind an actor.

Promotion is one signed Candidate with two ordered effects:

1. Injected `worker.profile.promote`, containing exact
   `previous_registration_id` and `registration` fields.
2. The unchanged built-in `worker.register`, containing the same registration.

The plugin requires `worker.register` authority and owns only the
`worker.profile.promoted` audit fact. It does not override or emit the built-in
registration fact. The audit binds Worker, actor/key, prior and new registration
IDs, prior/new versions, profile and Candidate ID. Its ordered causal parents
are the Candidate receipt, prior registration and paired new registration.
The generic injected-effect validator rechecks current facts and effective
keys inside the SQLite writer transaction. A changed head, active claim or
authorization rejects observably; no partial promotion is committed.

Exact replay proves the original receipt, ordered committed facts and their
ancestry before mutable eligibility checks. Repeated launches using the original
adapter version may reuse the promoted version only when its exact committed
history explains the current metadata. Reuse writes no new facts.

Local `WorkerHost` compositions capture the resolved registration version.
Local claim callers carry it through package selection to the existing SQLite
version fence. A previously constructed host rejects after a registration
change. Pull-only retrieval binds the current version without claiming to
execute that profile. `worker registrations WORKER_ID` exposes current metadata,
full history, promotion records and exact fact IDs without provisioning keys or
writing SQLite facts; its operational read requires Redis.

## Compatibility and limits

Ordinary explicit `worker register` semantics, built-in registration bytes,
external signed claim/renewal payloads and the schema21 reader floor remain
unchanged. Explicit registration retains its existing operator authority and
does not acquire this promotion-specific active-claim restriction. Claims are
never reset or migrated by promotion. Renew and submit check current actor/key
binding; they do not gain an invented persisted registration-version field.

Profile is descriptive compatibility metadata, not a capability grant, proof of
executed code or process isolation. Stop older local Worker processes before
promotion: readable schema21 records do not guarantee adapter matching across
mixed runtime versions. Explicitly unbound library compositions and external
signed claim callers retain their documented compatibility behavior.

Model-adapter setup, signed fixture execution and actual provider productivity
require separate evidence. A successful setup or idle result does not establish
successful model output or stable-release qualification.

## Alternatives and consequences

Using a new Worker ID would leave assignments pointing at the old identity.
Replacing metadata at the same version would destroy immutable history.
A blanket built-in registration restriction would change unrelated explicit
operator workflows. A preflight-only check would race with claims or disablement.
The paired plugin keeps this narrow application behavior at the existing
transactional commitment boundary while preserving the public identity.

## Related decisions

- [ADR-037: Worker coordination authentication](ADR-037-transactional-worker-authentication.md)
- [ADR-038: Task assignments](ADR-038-transactional-task-assignments.md)
