# Signed active-task handoff: development evidence

Date: 2026-10-04
Source version: 0.5.13.dev0; no release tag or stable qualification
Base revision: `01be3f4e585acd7975284a38e88dbdcbb44724c0`, with local development edits

## Snapshot and implemented behavior

The verified working-tree source snapshot has **391 files** and SHA-256
`25175d7aec04c47a27f57bfc1857da10963dd1689204794e374529af70556bd8`.
Its scope and hashing method match [report 074](074-signed-task-comments-2026-10-04.md):
Python source, tests, scripts and examples; example TOML and project/Compose
configuration; conformance JSON and script MJS files. Documentation is checked
separately for freshness and governance.

`task handoff/handoffs` transfers an active leaf business attempt through one
signed Candidate. Cancellation, old-claim release, bounded successor declaration,
allowance and attributed link facts commit atomically. Late old-claim submissions
are fenced. Optional successor holds prevent claim acquisition until exact release.
Only unfinished output slots are declared. Frozen execution/disclosure scope and
acceptance policy are inherited; completed slot names and exact Asset IDs remain
link metadata. Null-budget replay returns the original budget and successor.

Accepted injected projections are recomputed against current facts and effective
keys inside the SQLite writer transaction. Changed decisions reject before writes.
Concurrent key revocation leaves durable authentication rejection without trying
to authenticate the now-invalid receipt. Current views read Candidate-published
terminal facts directly. Polling late rejection for already non-failed terminal
work preserves its outcome and records unavailable recovery; failed-task recovery
remains available. Event metadata scopes the link to both source and successor.

## Verification

- Full serial suite: **1745 passed**, no skips, 6 dependency warnings, 66.71 s.
  Application and integration-test Redis used one isolated verification service;
  the normal Compose service and data were preserved.
- Ruff check and format check passed for 363 Python files, including the installed
  verification script. Commitment remains below its unchanged 300-line gate.
- Independent review and a 226-test focused run verified handoff, snapshot guards,
  late-rejection recovery and surrounding invariants. Final reference-scoping
  changes received a separate **105 passed**, no-skip affected-scope review;
  unchanged files were checked
  against the earlier 43-file SHA manifest. These runs overlap and are not summed.
- Independent two-connection probes verified concurrent partial output and signed
  actor revocation. Both reject handoff without link or source terminal; each
  leaves the appropriate durable rejection, without a thread exception.
- Forced two-connection behavior tests cover intervening terminal and unfinished
  child publication. Fault injection rolls back staged cancellation, successor,
  link and old-claim release. Permission checks, partial retention, independent
  successor acceptance, planner-origin work, historical cutoff and replay pass.
- Two public handoff vectors pass Python consumption and independent Node checks
  of canonical signing bytes, Ed25519 signatures and Candidate/runtime-record IDs.
  Existing comment and four hold vectors remain valid. These vectors do not prove
  Store concurrency by themselves.
- Builds and `twine check` pass. Fresh wheel installation outside the source tree
  passes actual CLI/Redis active handoff, partial retention, held successor claim
  refusal/release, late signed submission refusal, subsequent terminal-preserving
  polling, successor execution, exact replay, both task event views, reconstruction
  and reopen. All 181 packaged Python files match source bytes. Surrounding Workers
  are explicit fixtures; control, signing, commitment and Redis are actual paths.

## Retained failures and corrections

The first integrated focused run had two failures because fresh revalidation
rejected before older tests reached the Store claim guard. Those tests now
intentionally freeze both context reads to exercise the lower guard; separate
fresh-snapshot tests cover ordinary revalidation. No assertion was loosened.
Vector generation initially failed on nested immutable mapping serialization;
recursive wire conversion fixed it before public-vector consumption passed.

Installed execution found late rejection of cancelled work caused polling to
schedule recovery and write a conflicting failure trace. The task became stalled
although its cancellation fact remained intact. The failure was reproduced and
its workspace retained; terminal-preserving unavailable recovery fixes this path.
A new failed-recovery test initially assumed an incorrect parent relationship;
it now checks the existing recovery owner rather than changing that behavior.

An independent revocation probe found handoff was blocked but the rejection
could not persist because it reused a receipt for the revoked actor. The distinct
authentication-rejection path fixes observability without relaxing receipt checks.
The original failed probe and successful recheck remain retained.

A later installed event assertion examined only the default first 100 revisions;
the link was at revision 106. The verifier now requests sufficient history.
Successor-scoped events also needed an explicit generic `successor_id` reference;
that field is now recognized without parsing operator text. The final snapshot
was rebuilt and the complete suite and installed flow rerun after these changes.

An independent affected-scope run initially hit Redis preflight before the
malformed-trace assertion. The failed and filtered runs were retained. Rerunning
the complete affected set on the isolated Redis service passed all 105 tests
without skips or deselection, with the original trace assertion unchanged.

## Limits and continuing work

This slice refuses unfinished descendants, open predecessor holds, system/recovery
or plugin-controlled work and protected minting. It does not transfer an expanded
subtree or implement review/change requests. Cancellation does not stop an OS
process or reverse external effects. Ordinary outputs retain global same-name
matching: retained IDs do not prove predecessor production. The explicit handoff
reason becomes Worker context; retained contents are not automatically disclosed.
Independent successor outputs still need their own exact qualification.

The window between recovery's pre-schedule check and concurrent source cancellation
remains unfenced and unverified. It must be qualified before stable closure;
already-terminal late rejection evidence does not cover that window.

This slice has **zero provider calls**. Fixture execution does not requalify the
older live-provider report or establish complete Kanban parity. No commit, remote
update, tag or package publication occurred. The broader usability goal remains active.

Decision: [ADR-030](../docs/adr/ADR-030-transactional-task-handoff.md).
Change: [041](../changes/041-signed-task-handoff.md).
