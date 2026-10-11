# Local Worker entry development verification

Date: 2026-10-05. Source version: **0.5.13.dev0**. This records bounded
development verification of [local Worker profile promotion](../changes/051-local-worker-profile-promotion.md).
The deterministic and installed gates below pass. They establish adapter setup
and ordinary signed fixture execution, not real model productivity or stable
release qualification. The wider Kanban CLI goal remains open.

## Implemented behavior

An empty-domain pull-only Worker can acquire its concrete local adapter profile
under the same Worker ID and signing key. One signed Candidate pairs the plugin
promotion audit with the unchanged built-in registration effect; the prior
registration remains immutable. Automatic promotion requires the exact eligible
history, unchanged routing and identity, and no active claim, including an
elapsed claim still recorded as active. Repeated default setup proves the paired
history before reusing the resolved version and adds no facts.

Local hosts carry that resolved version through routing and writer arbitration.
A stale host is refused before it can claim under a later registration.
`worker registrations WORKER_ID` reads current metadata, history and promotion
facts without publishing anything. Redis remains required for operational
discovery and relay; SQLite remains the fact and claim authority.

Ordinary explicit registration, external signed claim/renewal shapes and
schema21 are unchanged. Profile metadata describes compatibility, not executed
code or isolation. Stop older local Worker processes before promotion; the
reader floor does not establish mixed-version adapter matching. See
[ADR-039](../docs/adr/ADR-039-local-worker-profile-promotion.md) and the
[operator guide](../docs/reference/task-board.md#inspect-local-worker-registration).

## Frozen source and deterministic evidence

The final 487-file code/configuration/vector/test map SHA-256 is
`f1dd65effa5c69dce4042ec88eb0b6bd7d44155fc9cc7b7145334f7d8645e289`.
Its 203-file runtime map is
`8c5b9acef71ea21efe6185fcf989b1bacec13394fd4fb4d6995960284a3a75ef`.
Ruff check and format pass. Serial full2 passed **1,979 tests**, six dependency
deprecation warnings and no skips, in 80.97 seconds; log SHA-256
`5f8a6cf9964e6e918ddff9b216eb256fd87ea9ab99da9dd997f31ff56117e4ea`.

Coverage includes both actual SQLite writer orders for claim/promotion,
registration and signer-key changes after projection, active elapsed claims,
foreign or disabled identity, unknown history, changed routing, exact replay
after later registration and rebuild, and repeated setup where the operator
and Worker share an actor but use distinct authorized keys. These checks do
not infer actor inequality as a permission rule. Python and the separate Node
consumer verify the language-neutral promotion signing and identity vectors.

Independent GPT-6 Luna review ran 113 focused cases on source1. Source2 changes
only two CLI test fixtures and the installed helper, leaving the runtime and
those 113 cases unchanged. Independent review verified that delta, ran its ten
fixture cases, checked the helper's strengthened receipt/commit provenance
assertions and consumed the Node vector. Review manifest SHA-256:
`d8e77393b787e775698b29c7028f6bbd2572946146dc69547968cea9bc4b4b80`.
Focused counts overlap other checks and are not added together. The root agent
ran full regression and installed verification; those are integration evidence,
not a second independent full suite.

## Installed artifact evidence

Build1 wheel SHA-256:
`04127cb15ca8888c08e6a54ba16910fc2a70cd957d742c1bc18f38e8d3a2c038`.
All 203 packaged Python modules match the frozen source. The full installed
CLI runner passed outside the source tree with fresh domains, offline dependency
installation and real Redis. Its main schema21 reconstruction preserves 536
records and the materialized digest; manifest SHA-256:
`0b679de8cb99f6f5053c069e079236161bb0004a804b64270c6b8dc59817c47f`.

The new entry fixture first registers a pull-only Worker, then performs two
local model-adapter setups against a request counter. It verifies one promotion,
two immutable registrations, unchanged Worker/key, no facts on repeated setup,
read-only discovery and exact receipt/effect/commit ancestry. Ordinary signed
harness output then completes a task claimed at version2. Typed Worker event
references and history survive SQLite and scoped Redis rebuilds unchanged.
Entry fixture manifest SHA-256:
`b1a5ebc3cfe9231ae185b6fc6bff86ee3326fc32eaad189b40e1828b81b3f53b`.
Model-adapter invocations and provider requests are **zero**. Fixture output
does not establish real provider output or independent policy qualification.

## Retained failures and open gates

The first full invocation retained **10 failures and 1,969 passes**. Review and
revision tests mocked an old Redis import binding and a no-argument mock instead
of the current optional-URL interface. Only the two test fixtures changed;
production code was unchanged for full2. The failed log SHA-256 is
`3de9820e843858c5bf7f235d42dcceb829e5431ef1a356e01d997267d62cd3df`.

Earlier source probes retained two root helper preparation errors: a tuple/list
comparison and a nonexistent submit option. A later probe passed through the
documented signed submit interface. A separate valid same-actor probe exposed
an overrestrictive history check; the implementation was corrected and reviewed
before the final freeze. Failed probes remain diagnostic evidence rather than
being counted as runtime acceptance.

The prior actual Flash call in [report 082](082-task-assignments-2026-10-05.md)
returned HTTP401, with unknown usage/cost and no accepted report. No provider
retry was made for this repair; the credential's API Base URL remains to be
confirmed. Priority and the remaining Kanban capabilities require their own
implementation and acceptance. No release tag, remote update or publication
was performed for this slice.
