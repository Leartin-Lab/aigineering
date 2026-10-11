# Policy-bound task review and descendant fencing: development evidence

Date: 2026-10-04
Source version: 0.5.13.dev0; development qualification only
Base revision: `01be3f4e585acd7975284a38e88dbdcbb44724c0`, with local edits

## Snapshot and behavior

The verified working-tree source snapshot contains **402 files**, SHA-256
`87300cea76ebdd9618d4eb8841e70384affdbc69253467a584d481792baea236`.
Scope and hashing follow [report 074](074-signed-task-comments-2026-10-04.md):
Python source/tests/scripts/examples, example TOML, project/Compose configuration,
conformance JSON and script MJS. Documentation freshness is checked separately.

Review requests reserve source allowance and declare ordinary report tasks with
exact original promptable target/rubric/evidence context. Signed decisions check
report lineage and producer separation, then delegate to canonical attestation.
Reports cannot widen frozen policy evidence. `accept` qualifies eligible output;
`changes` leaves it unqualified. Exact replay precedes mutable checks. Custom
decision keys need existing authorization; the local human-verifier role is fixed.

Shared `run --worker-id` selects durable identities for quick, pool and task/resume
execution. Configured fleets refuse overrides. Distinct actors establish
attribution, not independent people or models.

Independent review reproduced a shared completion defect: cancelling only direct
children left grandchildren claimable. The pure FactReducer now traverses every
level through satisfied or already-terminal intermediates, preserving original
terminals and branches serving a live outer independent policy. Existing
transactional materialization releases claims and fences late submissions.

## Deterministic and installed verification

- Full serial suite: **1790 passed**, no skips, 6 dependency warnings, 68.61 s.
  Operational tests use isolated Redis; flush tests use a separate database.
  The default Compose service and data are preserved.
- Ruff check and format check passed for 373 Python files. Commitment remains
  below the unchanged 300-line gate.
- Independent read-only review passed **62 focused tests**, bound to a 15-file
  SHA manifest. A later architecture addition passed **16 tests** and updated its
  file hash. These overlapping runs are not summed. No explicit authority,
  disclosure, lineage or replay defect was identified.
- Two-connection tests force competing review reservations, source completion
  and both winning decision verdicts; losers leave durable rejection. Exact
  report ancestry, foreign same-name output, sealed context, producer separation,
  custom key permissions and terminal replay pass.
- Cascade tests verify actual grandchild claim release and pre-signed late
  submission refusal, immutable terminal intermediates and outer verifier
  preservation. Architecture checks exercise both completion entries with durable
  mutation guards. These are not barrier-based cancellation-versus-submit races
  or concurrency stress evidence.
- Four review vectors pass Python pure projection and separate Node signing-byte,
  Ed25519 and Candidate/runtime-record identity checks. Node is not a second
  runtime implementation or Store concurrency test.
- Wheel and sdist builds pass. Installation outside the source tree exercises
  actual CLI/SQLite/Ed25519/Redis request/report/accept/changes, replay, reopen,
  event scoping and rebuild. Workers are explicit fixtures with **zero provider
  calls**. Existing handoff, holds, configured-run, artifacts and reconstruction
  checks also pass. Final artifact inspection checks packaged source bytes,
  document freshness, privacy and metadata against this snapshot.

## Real provider execution and content outcome

Two **DeepSeek Flash** calls used distinct `repository-producer` and
`repository-reviewer` identities. Frozen input contains only README, task-board
reference and ADR-031, plus a declared rubric. Reviewer disclosure contains the
exact target, rubric and original evidence. No private/sealed input or tool
capability is included. Documents changed after capture; the captured bytes
remain the evidence for these runs.

Producer usage: **8747 prompt + 3422 completion = 12169 tokens**.
Reviewer usage: **12411 prompt + 1976 completion = 14387 tokens**.
Total: **26556 recorded tokens**; allowance is separate from billing. Both Worker
submissions committed. Producer exit 1 indicates pending independent acceptance,
not a failed HTTP invocation.

The target omitted independent-policy declaration from its claimed usable command
sequence and presented README non-goals as promised development. A separate
read-only content review confirmed both defects. The model reviewer recommended
changes but incorrectly said review preceded producer execution and suggested
an exact-ID form unsupported by `asset show`. Its text was checked against the
original target and was not automatically treated as truth.

The human decision records **changes**, identifying confirmed defects and rejected
reviewer assertions. Reopen and rebuild preserve exact request/decision and both
task event scopes. There is one rejected attestation and no source qualification.
Source result export refuses the unqualified output; the ordinary reviewer report
exports independently. This is real review/return evidence, **not a successfully
independently accepted repository report**. Original outputs and negative content
outcomes remain retained; revision work was not fabricated with a fresh root grant.

## Retained verification problems

Early identity checks hit sandbox TCP refusal and one nonexistent Worker property
assumption. Dedicated Redis access and the actual model property resolved them.
Replay fixtures initially included commit-derived facts outside the original
Candidate head; they now check immutable projection IDs and no extra qualification.
Other fixture failures used a closed Store or one Worker for two active claims.
Canonical wire conversion fixed tuple-versus-array vector comparison. The source
CLI verifier also corrected its assumption about the existing event-list envelope.

The first full run passed 1783 tests but skipped 7 Redis cases because the test-only
address was missing. The final retained run includes all seven. An opaque broker
launch was automatically rejected before execution. Fixed public-only manifests
and complete prompt previews were checked; explicit direct CLI calls to the
declared endpoint were then approved. That preflight made no provider call.
Failures and negative content samples were not discarded or retried until success.

## Remaining limits

This slice supports live independent-policy business sources. `changes` does not
publish revision work, reset submitted claims, reopen terminals or implement
ordinary advisory review. Report completion retains global same-name semantics;
decision independently rejects foreign report provenance.

Historical trees missing descendant cancellations are not migrated by the reducer
fix or view rebuild. Authorized repair remains a stable gate, alongside the
recovery precheck-to-publication cancellation race in report 076. Expanded handoff,
assignment/priority/dependencies, bulk operations, multiple boards and persistent
dispatch/notifications remain in the broader Kanban goal. No stable release,
commit, remote update, tag or package publication occurred.

Decision: [ADR-031](../docs/adr/ADR-031-policy-bound-task-review.md).
Changes: [042](../changes/042-signed-task-review.md),
[043](../changes/043-completion-descendant-fencing.md).
