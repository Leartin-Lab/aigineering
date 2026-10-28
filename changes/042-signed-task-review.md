# Policy-bound task review through ordinary report work

Date: 2026-10-04
Source version: 0.5.13.dev0; development only, no release tag
Status: Completed development slice; stable qualification remains open

`task request-review` creates a bounded ordinary reviewer child and an attributed
immutable link to one exact committed output and its frozen independent policy.
Only original promptable target, rubric and evidence Asset IDs enter its context.
The reviewer uses the shared Worker path without tools or delegation.

`task review decide` checks the exact committed report, reviewer ancestry and
producer separation, then delegates accepted or rejected attestation to the
existing policy owner. Reports remain separate linked evidence and cannot expand
the original policy. `task reviews` exposes original requests and decisions.
Exact keyed replay precedes mutable-state and duplicate-verifier checks; generic
writer revalidation arbitrates competing decisions and allowance reservations.

Shared `run --worker-id` selects durable execution identities for quick, pool and
target/resume runs. Configured fleets retain their configured identities. The
option grants no verification capabilities. Custom decision identities require
existing authorization and a matching private key; local defaults grant only the
fixed human-verifier role.

This slice supports live independent-policy business tasks. `changes` records
rejected attestation but does not create revision work. Ordinary advisory review,
expanded handoff and revision publication remain future work. Distinct signed
actors do not prove separate people or models; human decisions remain assertions.

Decision: [ADR-031](../docs/adr/ADR-031-policy-bound-task-review.md).
