# Bulk task reassignment and clear verification

Date: 2026-10-08. Source version: **0.5.13.dev0**.
This bounded CLI slice does not qualify the whole Kanban goal or stable release.

## Implemented behavior

`aig task reassign CONTRACTS...` replaces one common Worker binding or clears
bindings for 2 through 64 targets. Every target supplies one exact, nonempty
`--previous-for CONTRACT=HEAD`; the single-target `--previous` form retains its
result, direct key and Click missing-option exit behavior. A clear fact is a
nonempty history head for later reassignment. No latest-head discovery is used.

Global shape validation precedes Store, identity and required Redis access.
Each target delegates validation and publication to the existing assignment
owner with its exact head and `require_assigned=True`. Keys bind action, base,
exact Contract and predecessor, independently of order or sibling subsets.
Every row echoes `requested_previous_assignment_id` as input context; this is
neither committed evidence nor authority. Known refusals preserve successful
siblings. Each target remains independently atomic.

Exact-data accepted relay-pending results retain their IDs and key and continue
to the next callback. Sustained outages prevent later publication during normal
pre-sync; recovered relay may permit it. Accepted pending without exact data and
unknown exceptions stop with an unattempted tail and a safe exception class.
The existing collector, application, plugin, kernel and schema remain unchanged.

## Frozen source evidence

The **566** source/config/test/helper-file map SHA256 is
`e6f74f5d01f31e4eba015a3429e177651a83cfc198ba645a7beee2586f2590dd`.
The **217** runtime-file map SHA256 is
`47dccb236ffc975591341d856c51db14707103f806abe675b37c47da24b223ff`.
Source, public documents, report data and root guide were unchanged across the
root full run; this report and its README link were added afterward.

- Root serial full suite: **2341 passed**, six existing dependency warnings,
  113.77 seconds, isolated Stores and native Redis.
- Root Ruff and format checks passed for **483** source/test files.
- Author focus: **49 passed**; independent focus: **129 passed**, using mocked
  collaboration. These overlapping fixture counts are not added and do not
  imply real provider or native Redis execution by either agent.
- Separate independent temporary SQLite controls injected `KeyError`,
  `TypeError` and an accepted pending carrier without exact result data.
  They verified unknown-stop, safe output, every row's requested head and
  call counts of two, two and one respectively. They are independent controls,
  not additional permanent adapter regression tests.

## Installed acceptance

The first source-external packaged CLI run passed **141** phases, including
**6** expected nonzero exits. Nine ordinary task declarations and idle Worker
registrations supplied signed metadata fixtures; no Worker or provider was
invoked. Native Redis and a selective local assignment relay fault were used.
All original binary command streams were retained privately with verified
hashes and 0600 modes.

The run exercised set and clear as separate homogeneous batches with mixed
known refusals, reordered subset and singleton actual-key replay. Exact original
keys returned original facts after later assignment heads, and a changed mapped
head generated a distinct intent. A clear head remained valid for a later set.
Global invalid shape and required-Redis preflight left all **18** SQLite table
fingerprints and identity-file contents unchanged.

Under the sustained relay fault, the first assignment's exact fact, Candidate
receipt and committed head were proven durable while its native feed event was
absent. Both later targets retained their full seeded history, head and Worker.
Singleton recovery was SQLite-neutral and caught up the original event. Fresh
later siblings then committed normally; the subsequent full reordered replay
was neutral and preserved every member ID. Exact member events were checked in
native Redis. Reopen/rebuild retained canonical assignment history and the
runtime materialization digest. These checks do not establish business output.

The installed wheel SHA256 is
`f2708ab4c919350d7dfcdbf0a07ff9b0be0a0540e02513616dc65be0a76fbcf0`.
Helper V2 SHA256 is
`92312129fae46371989d2fab5e8db10f945bfa0c5c168238ce0f37e7bf7032da`.
The wrapper separately checked exclusion of the actual source checkout;
the helper checked same-venv CLI/Python site-packages binding.

## Retained failures and limits

The tests-first run rejected new multi-target syntax before implementation.
The author's later 48-pass/one-failure run used a quote-sensitive architecture
assertion and was corrected separately. System Python lacked pytest for the
author and `_pytest` for an independent probe; setup failures remain distinct
from behavior failures and passing repository-interpreter runs.

Helper static fixtures first omitted the required requested-head echo, and a
later AST assertion used the wrong quote spelling; their failed streams remain
retained. Root source inspection found the V1 pending oracle incorrectly
required empty sibling history even though reassignment targets already had
initial assignments. Before any installed invocation, V2 was changed to compare
the complete seeded projection, retain unattempted siblings and allow exactly
the first returned accepted append. Independent positive and negative controls
passed. One independent helper-review script syntax failure has transcript-only
evidence without original private raw logs; no passing run fills that gap.

Root's first post-run metadata assertion looked for table count in the helper's
return instead of its inner manifest and failed with `KeyError`. This read-only
oracle mistake has transcript evidence and a separate late diagnostic; the
corrected field-path check passed without repeating the installed operation.
There was no failed operational installed run for this slice.

Malformed UTF-8 and programming-error injections have source fixture coverage;
current-key/claim/terminal constraints also have existing-owner source coverage.
The 64-target input shape was checked before preflight, but an actual signed
64-target batch was not run. Installed human output, operator-key revocation and
a stale-head race injection were not run by this helper. The installed relay
fault is not native Redis shutdown. Report097's earlier 64-target wording was
also clarified; its original frozen evidence remains unchanged.

Current-key, claim, terminal and writer constraints remain owned by the existing
signed runtime; source fixture evidence and installed cases have distinct scope.
The earlier real-provider repository report belongs to its frozen historical
runtime and does not qualify current composition. Bulk priority/retry, boards,
persistent dispatch, schedules, notifications, retention/unlink, PR adapters
and remaining stable gates stay open. See
[change 068](../changes/068-bulk-task-reassignment.md) and
[the command reference](../docs/reference/task-board.md#assign-exact-work).
Local edits imply no commit, remote update, tag or package publication.
