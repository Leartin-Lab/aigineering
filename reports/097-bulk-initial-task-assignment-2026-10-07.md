# Bulk initial task assignment verification

Source checks: 2026-10-07. Installed acceptance: 2026-10-08.
Source version: **0.5.13.dev0**.
This bounded CLI slice does not qualify the whole Kanban goal or a stable release.

## Implemented behavior

`aig task assign CONTRACTS... WORKER_ID --reason TEXT` accepts up to 64 exact
Contracts for one registered Worker. One Contract retains its original output
and direct key. Multiple targets use the existing signed application owner once
per target, preserve input order and report independent partial results. The
Worker, reason and empty predecessor remain bound to each fresh assignment.
Capacity still gates claims, not queued placement. No kernel, plugin, schema,
claim authority or scheduler is added.

Global shape validation precedes Store, identity and required Redis access.
Actual target keys are derived from the batch base and exact Contract ID,
independently of order. Reordered batches and subsets of two or more use the
original base; one member uses its returned actual key through single syntax.
Known typed refusals continue. Unclassified failures stop and preserve the
exception chain while exposing only its safe class. Deliberate assignment
application errors retain ValueError compatibility.

Accepted relay-pending results carry their exact IDs and key. A sustained relay
failure can prevent subsequent assignments before publication. That target's
`not_attempted` result does not prove that separate authorization facts were
never committed. An accepted carrier without exact result data is unknown and
stops later calls; the collector no longer continues this previously missed
case. Human output avoids echoing unsafe batch bases; JSON preserves exact keys.

## Frozen source verification

The **563** source/config/test/helper-file map SHA256 is
`4087d9b192dfb6a2e10fd4af1165af13de6180428db3b68d3c84cde599495a60`.
The **217** runtime-file map SHA256 is
`17af229ae5985c73b9c0e234f411f2fc4ba9de3d467116202f31eebe5627513e`.
Source, public documents, report data and root guide remained unchanged during
the root full run. Its helper was version V2. The subsequent **563**-file map
`187574a33083ac3a548e29fbcfa73a4db77826926ab28c454f139314da0f6206`
differs only in `scripts/installed_bulk_assignment_smoke.py`; runtime and tests
are unchanged. That helper delta passed independent static controls and the
separately recorded installed run below, rather than another full-suite run.

- Root serial full suite: **2321 passed**, six existing dependency warnings,
  113.65 seconds, isolated Stores and native Redis.
- Root Ruff and format checks passed; **481** source/test files checked.
- Author focused regression: **115 passed, 1 skipped** without explicit native
  Redis. Independent focused execution: **116 passed**, including that native
  Redis test. These overlapping counts are not added.
- An independent pure three-target control confirms only the first unknown
  target is invoked, later targets remain unattempted, actual keys survive,
  JSON remains one document, typed refusals continue and Click flow is preserved.

## Installed acceptance

The second source-external packaged CLI run passed **82** phases, including
**5** expected nonzero exits. It used ordinary signed task declarations and
Worker registrations with native Redis and a selective loopback relay fault.
No Worker or provider was invoked; these are metadata fixtures, not completed
business output. Exact original binary streams remain retained with hashes.

The installed run verified legacy single syntax, partial results after known
refusals, explicit/generated bases, reordered subset and singleton actual-key
replay. Invalid shape and required-Redis preflight left all **18** SQLite table
fingerprints and the local identity directory unchanged. The first relay-pending
assignment's exact receipt, head and fact were proven durable before recovery,
while its native feed event was absent. Under that sustained fault, later
targets remained unattempted. Singleton replay synchronized the original fact
without changing SQLite. Resuming untouched siblings legitimately added their
own facts; subsequent full replay was SQLite-neutral and preserved all IDs.
Every pending-batch member's exact event ID and revision was checked in native
Redis. Reopen/rebuild preserved assignment history and materialization digest.

The installed wheel SHA256 is
`dffd4002655111a77a070770556f2afa88c6098649143b0175ddcf2f7c7931ac`.
Helper V4 SHA256 is
`ff6aa4f4d5bbc03973c3e58076ad45aac2e1648428885cc9aa8813499c893a8b`.
The source-external wrapper separately proved exclusion of the actual checkout;
the helper checked the CLI/Python same-venv site-packages binding.

The first installed run failed in the helper's embedded installation-origin
Python snippet before domain initialization or task publication. Installation
itself succeeded; exact failed streams remain retained. Module lint/import
checks had not compiled this embedded snippet. Finite corrections compiled
embedded programs, repaired the shared-work-parent path check, and included
the third target accessed by a projection oracle. That last mismatch was found
by static review before the second installed run. V2/V3 bytes remain preserved.
One automatic approval invocation could not complete because of account quota;
it started no command. A later read showed restored availability and the same
operation passed normal approval. This was separate from both executed runs.

## Retained failures and limits

The tests-first run retained four failures and one passing control for missing
variadic syntax and narrow exception collection; the system-Python pytest setup
failure is separate. Root's pure callback reproduced continued calls after an
accepted carrier with missing data. The existing test encoded that continuation
and was narrowly corrected to require a single call and an unattempted tail.
Original streams and prior test bytes remain retained.

Independent source probing first used an incorrect one-line JSON assertion
against the valid pretty printer; that failed stream remains separate from the
corrected passing probe. Helper static drafts had a syntax correction and a
duplicate-ID synthetic fixture failure, not installed/API/Store failures.
Independent helper review found that a contradictory unattempted row passed
its oracle and that later sibling Redis IDs were not individually checked.
The finite helper repair and its final review are recorded separately.
Intermediate scanner expectation failures are retained; three reviewer-control
failures have tool transcript evidence without original private raw logs.
No passing run fills missing historical streams.

Human output, malformed UTF-8, programming-error injections and the
current-key/claim/terminal fence matrix remain source-only controls. Source
fixtures validate the 64-target input bound; an actual signed 64-target batch
was not run. This wording clarifies the earlier public scope statement without
changing its frozen historical evidence. The installed fault is a selective relay failure, not native Redis
shutdown. The broader real-provider business evidence belongs to earlier
snapshots and does not qualify current-runtime composition.

Reassignment, priority and retry remain separate bulk slices. Boards, persistent
dispatch, schedules, notifications, retention, current-provider composition
and remaining stable gates stay open. See [change 067](../changes/067-bulk-initial-task-assignment.md)
and [the command reference](../docs/reference/task-board.md#assign-exact-work).
No commit, remote update, tag or package publication is implied.
