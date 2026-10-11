# Task execution dependency development verification

Date: 2026-10-04. Source version: **0.5.13.dev0**. This verifies
[signed execution dependencies](../changes/048-task-execution-dependencies.md),
following [report 079](079-expanded-root-handoff-2026-10-04.md).
It does not qualify a stable release or complete the continuing Kanban CLI goal.

## Implemented behavior

Signed `task link` / `task unlink` decisions place or release one exact immutable
claim constraint; `task dependencies` reads its history. Only typed successful
completion of the exact prerequisite permits a new target claim. Failed,
cancelled, unreachable or missing prerequisite terminals remain observable
blockers. SQLite checks the same pure predicate inside the claim writer transaction.
Links do not provide input Assets, change frozen Contracts or override output
qualification. Dependencies are scheduling constraints, not automatic data flow.

A single core owner validates provenance, edges, duplicates, cycles and historical
blockers. Plugin intent and application publication share generic commitment and
writer revalidation. Self-links are refused before operator provisioning. Signed
handoff/revision preserve exact historical edges and reject selected targets with
unfinished external prerequisites; no automatic repointing or recursive cancel.
Manual hold, exact unlink, new link and exact hold release are four separate
signed decisions. Redis exposes both endpoints and rebuildable history; it is
required operational infrastructure and is not claim authority.

Schema 20 prevents normal reopen by readers that ignore these constraints. Valid
schema 19 history retains records and IDs; unsupported earlier constraint rows
refuse upgrade with the backup retained. Stop old Workers. Already-open older
connections remain outside this version gate. See
[ADR-036](../docs/adr/ADR-036-task-prerequisite-claim-constraints.md) and the
[operator guide](../docs/reference/task-board.md#order-work-with-exact-prerequisites).

## Deterministic, independent and compatibility evidence

The final 464-file code/configuration/vector/test map SHA-256 is
`08a51804c42c9b364e47299f87e05e09f39cb7c13a6cf7b47af293635b1ac588`.
Its 197-file runtime map is
`5607f034d29ede8226190f1c7c5511a88a3fe09ce045d4f8143eb758e335e247`.
Integrated lint/format passed. The serial full suite passed **1,903 tests**,
with six warnings and no skips, in 73.90 seconds. Earlier passing snapshots were
retained separately and do not validate later runner changes.

Independent GPT-6 Luna core review passed 71 focused checks, including a real
writer-predicate rejection probe; manifest SHA
`ed70bde840a584d9ca6504386fcfe0145b842a10a1270c2a710dfdee86a65bd0`.
Combined independent review passed 59 focused CLI/plugin/race/HTTP/Redis/conformance
checks, two neutral Node effect vectors and 48 governance checks; manifest SHA
`dd006c96b4a911db3b8f876452d027eba12638051d33b04b01e1ec7105b3f586`.
These runs overlap and are not one independent 130-test suite. Author checks are
recorded separately. The later installed-smoke failure-evidence patch leaves
runtime source unchanged and has separately bound independent five-test delta
evidence; manifest SHA
`083bf9381f83adf6ccef459332ab642dc5db882271d8678b046b270f323b4d33`.

Barrier tests cover opposite-edge cycle arbitration, claim-first/link-first,
writer snapshot changes, cancellation/repair boundaries and exact replay. HTTP
checks claim parity; this does not introduce a dependency mutation HTTP endpoint.
All eight neutral vector suites pass. Node validates canonical bytes, signatures
and IDs; it does not prove SQLite concurrency.

A readonly backup of a retained real schema 19 domain with 127 records and three
allowance returns upgrades to20 with unchanged IDs/content. The genuine earlier
reader refuses20 even without dependency facts. A separate unsupported pre-20
constraint import refuses migration, retaining version, records and backup.
The original retained real domain remains unchanged.

## Installed artifact and failed verification

The isolated build2 wheel passed the complete installed CLI smoke outside the
source tree, using actual Redis and fresh domains with explicit fixture Workers
(provider calls: 0). It has the same 197 runtime sources as this final feature.
Direct claim blocking, prerequisite/target completion, exact post-terminal replay,
result export, both endpoint events and Redis rebuild equality passed. The main
reconstruction retains 505 records under schema 20. Wheel SHA-256:
`6560bfbb6759a99c160adbc272383124fadfdf449ec30c643a650e6ae70956c4`.
The final documentation/runner wheel also passed a unique full outside-source
installation using the reviewed failure-evidence script, with a fresh evidence
directory. It retains the same 505-record reconstruction and dependency checks.
Tested final wheel SHA-256:
`8dc4fca578048912f766ddc2d9deb192a28f1a334ae62ac2d2692894a45d9421`. Final source/document equality and artifact privacy are checked
separately; no source changes are implied by this packaging step.

One installed verification was mistakenly started twice with a shared evidence
directory. Different retained workspace references and timestamps, plus a
preserved-database probe returning FileExistsError/exit2 for the existing directory,
strongly support that cause. The original child streams were not retained;
this is a causal reconstruction rather than a captured original exception.
Existing passing files belong to another invocation and do not validate the
failed one. Fresh-directory diagnosis and an isolated full installation passed.
The failure remains retained. The runner now preserves nonzero diagnostics in
unique mode0600 files inside that invocation workspace, keeping raw streams out
of the caller repository, other evidence directories and console summaries.

## Actual work and content limits

Two bounded ordinary tasks used real DeepSeek Flash, each with allowance 1,
no tools, no child planning and a frozen exact public input Asset. The first
produced a Chinese operator guide; the second produced an operational checklist.
The target was visibly blocked before prerequisite completion, became ready
and was claimed afterward. Fact revisions are guide claim 56 / complete 67 followed
by target claim 77 / complete 89. Two provider calls report 18342 and 22664 tokens,
**41,006 known tokens**, with zero provider retries. This measures these calls,
not money, universal reliability or independent semantic qualification.

Both raw Markdown results are exact exports of committed ordinary Assets.
The guide SHA-256 is
`33d4f77c702abf013c09ff592678a5399885bac12aab12cd409063e7e35539b0`;
the checklist SHA-256 is
`56571a2b0a48dff56f53e953189fa8129f6915fe83efb3757a10e3711f0b9676`.
Independent content review found the checklist usable. The guide omitted an
existing-workspace upgrade branch and overstated claim/acceptance requirements
for operator effects while calling a readonly query signed. A separately reviewed
human editorial copy corrects these statements; it is not the original committed
Asset and has no canonical independent-policy qualification. README's same
signed-query wording was corrected. Frozen provider input remains retained.

Preparation failures remain explicit: a truncated terminal input, malformed
acceptance policy and activation syntax, and one misprepared v3 task signed-
cancelled before any claim. Correct exact bindings were then published in a new
v6 task. These were operator preparation failures before provider invocation;
no failed provider call was discarded. 93 retained records, including the cancelled
task, survive reconstruction on a copy. Terminal link replay returns its original
placement; Redis events contain both endpoints and dependency history is equal
before/after domain-scoped rebuild. No database flush or read-only completion repair.

Assignment/reassignment, priority, edit/promote and later bulk/boards/dispatch/
notification/retention work remain in the full goal. Stable qualification also
requires current-snapshot recovery/concurrency and independent-acceptance gates.
No tag, remote push or package publication is implied.
