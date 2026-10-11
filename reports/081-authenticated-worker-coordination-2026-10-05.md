# Authenticated Worker coordination development verification

Date: 2026-10-05. Source version: **0.5.13.dev0**. This records
[authenticated coordination](../changes/049-authenticated-worker-coordination.md)
after [execution dependencies](080-task-execution-dependencies-2026-10-04.md).
This is bounded development verification, not stable-release qualification.

## Implemented behavior

Operational local and HTTP claim/renewal paths carry the original signed
Candidate into SQLite's writer transaction. One shared owner checks exact
payload shape, current effective key and capability, enabled Worker registration
actor/key binding and the requested transition. Claim preparation also retains
its registration-version fence. Canonical request previews are compared by
identity and effective content, ignoring only observation time. A failed writer
check rolls back without partial claim, lease or request facts.

The prior receipt writer already checked revocation. The reproduced defect was
a non-revoking registration rotation between preflight and claim preparation:
the old key's receipt could accompany the new registration version. The current
writer rejects that combination. Local run/fleet, Engine, method and CLI callers
now use their actual Worker signer through the same compiler as harnesses.
Lease keeping signs renewal and joins before failure writes on its connection.

Successful signed requests are single use. A null-target scan finding no work
records nothing and can be retried. Signed grants reference the exact request;
renewals reference both the original grant and exact renewal request. The
request references its Candidate receipt. Trusted recordless Store/library calls
retain their legacy shape and are not operational authentication evidence.
Schema20 is unchanged. See [ADR-037](../docs/adr/ADR-037-transactional-worker-authentication.md).

`worker next --candidate JSON` takes inline signed JSON. Signed values are used
when flags are omitted; explicit mismatches fail before mutation. Existing
foreign registrations require a matching signer or supplied Candidate, without
creating a replacement key. Expected authorization errors produce human Click
diagnostics or the JSON error envelope.

## Source and deterministic evidence

The final code/configuration/vector/test map has 467 paths, SHA-256
`d3c87e4f65703643f274f3095a9fbc8dd265300124e512451a31722c4e304890`.
The 197 runtime-module map is
`0fdb536fa7ee4adcda0923a71cc997dee139409c06f1d38e8af2a2ddb4fc9259`.

Author focused checks passed 216 tests with one upstream Starlette warning.
Independent GPT-6 Luna review also passed 216 focused production-caller and
ownership tests with one warning; these overlap and must not be added together.
The independent source/evidence manifest SHA-256 is
`858976116b5fd9de27faefc87e976f9b70f1bb5717347e98f2151cab80acbe77`.
The subsequently corrected quick-run fixture passed four focused checks; the
installed-helper replay delta received separate static independent review.

The initial integrated suite retained **1,919 passed / one failed / six warnings**.
The failure was an existing quick-run expansion/recovery test calling a changed
helper with `worker_id` rather than its actual WorkerHost. Updating both calls
preserves signed execution and the original descendant/recovery assertions.
Earlier author failures involving unsigned test Workers, outdated mocks and
selection of the first receipt instead of an exact output's causal receipt are
also retained separately. Passing later runs do not erase these failures.

A second integrated run retained 1,919 passed / one failed / six warnings: all
behavior tests passed, but the new ADR referenced a nonexistent local document.
That document link requires correction and a final frozen-document check.

After correcting the link and freezing the documents, the final serial suite
passed **1,920 tests**, with six warnings and no skips, in 75.92 seconds.
Ruff check and format passed for 415 source/test/script files. All eight existing
neutral Node vector suites pass; they do not establish SQLite concurrency or
constitute a new coordination vector. Post-document governance passed 48 checks,
and the root agent guide's 47 direct references resolve.

## Installed execution and compatibility

An isolated wheel installation outside the source tree passed the complete CLI
smoke with actual Redis, fresh domains and explicit fixture Workers. Provider
calls: **0**. The tested wheel SHA-256 is
`4da0cc5f89fe0efd0c5cf6e933b2c7198b12e1b23838c844459cda2a8122d84a`.
Its runtime sources match the module map above. The final wheel installation
includes the current README; source/document equality and privacy are checked
separately. Build and Twine checks passed for that artifact.

The new coordination fixture proved an empty scan writes no facts, the same
Candidate can claim later work, and successful replay leaves all fact IDs
unchanged with a second eligible task still unclaimed at spare Worker capacity.
Signed renewal replay leaves the deadline unchanged. Harness and local pull
results use ordinary signed submission and exact exported output checks.
Configured run and fleet also complete through their normal entry points.
Reopen/rebuild preserves coordination fact IDs; scoped task and global event
feeds are equal after domain-scoped Redis reconstruction.

The full installed reconstruction retains 536 runtime records under schema20,
with unchanged immutable record IDs and digest. Derived table timestamps are
not claimed byte-identical. Installed manifest SHA-256:
`1aa1f50108f3380268ba53f507394dbb729694a68ea467a5f8cf08cec9e14d3a`.
Coordination evidence SHA-256:
`5f3751cd32e8628e3da06bc9c2f6cc095707a384858053b650d6c7575e6e6fc4`.

## Limits

No real provider was invoked for this mechanical authentication slice. Earlier
DeepSeek evidence stays bound to its earlier source. New-source actual work and
independent policy acceptance remain required for the continuing goal. Trusted
Python/SQL access is outside remote authentication and process-isolation claims;
the existing floor cannot stop an already-open older connection.
Schema20 also does not distinguish earlier schema20 binaries: sharing the
database does not update their authentication code. Use updated operational
Workers and entry points for these checks.

Assignment/reassignment, priority and later bulk, boards, dispatch, notifications
and retention remain unfinished. Broader recovery/concurrency and stable gates
remain open. No tag, commit, push or package publication is implied.
