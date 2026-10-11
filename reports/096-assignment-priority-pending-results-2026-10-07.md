# Assignment and priority recovery results

Date: 2026-10-07. Source version: **0.5.13.dev0**.
This is a bounded reporting repair; whole CLI/stable qualification remains open.

## Behavior

`task assign`, `task reassign` and `task priority` construct the exact durable
result before post-commit Redis relay. Successful and accepted-pending results
include the actual idempotency key, Candidate ID, direct fact IDs, legacy data
and observed SQLite revision. Accepted pending exits nonzero without losing
those identities. Restore Redis, retain the original arguments and predecessor,
and replay using the returned key. SQLite remains the fact and claim authority.

Pre-publication failure has no operation result. A retained rejected decision
can report pending audit synchronization and exact rejection IDs, with
`candidate_accepted: false`; it is not an accepted assignment or priority.
Unexpected errors in these mutation commands return a fixed safe internal error
and preserve the cause internally. Missing-workspace and Click control flow
retain their existing behavior. Human pending hints display portable keys;
other legal explicit keys are not echoed raw and remain exact in JSON.

Kernel, plugin effects, schema, signing, claim fences and priority eligibility
semantics remain unchanged. This reporting repair does not redefine application
calls that discover a current predecessor when none was supplied. Recovery uses
the original CLI intent and its exact predecessor, not newly discovered heads.

## Frozen source verification

The **560** source/config/test/helper-file map SHA256 is
`dc43138978553aa29616c59b4bb4d9f11b45452a4a07dc2dc626a38782899894`.
Its **217** runtime files have map SHA256
`0cc3870b4ce4bf6f4c98aa5e688296f1aa469c2b093261d17a2991e4a14d9a61`.
Source, public documents and root guide stayed unchanged through the full run.

- Root serial full suite: **2305 passed**, six existing dependency warnings,
  113.88 seconds, isolated Stores and native Redis.
- Root Ruff and format checks passed; **479** source/test files checked.
- Author focused predecessor: **96 passed, 2 skipped**; the skips were existing
  native Redis checks without an explicit dependency. Only the final exact
  rejection-ID test assertion changed afterward; its two cases passed separately.
- Independent focused predecessor: **96 passed, 2 skipped**; the two native
  Redis tests passed in a separate isolated run. Independent final test delta:
  **2 passed**, exact returned rejection IDs compared with their durable record.
  Product source remained identical. These overlapping counts are not added.

Source controls cover generated and explicit keys, original predecessor
preservation, durable accepted results before retry, exact replay, preflight,
rejected-audit pending, local construction failure, chained safe internal errors
and human key display. Current-key and claim/terminal fences retain their
existing tests and implementation ownership.

## Installed acceptance

Offline installation outside the source tree passed **65 command/control
phases**, including six expected nonzero outcomes: two preflight probes, two
post-commit pending results and two changed-intent/stale-predecessor refusals.
The wheel SHA256 is
`aa3f77d60dbb2ef0a4a3b6094e9d2c8979f2eea9d7148b5c6ce9454fa16e4da4`;
packaged helper SHA256 is
`665d4d6dbe8bf57ae941cc26a777441a8411aee6212049b31223ca8d02482a4f`.

Ordinary CLI initializes a domain, registers two public Worker identities and
creates two fixture Contracts. Initial assignment generates a key and replays
it exactly. Reassignment and a priority update begin with existing heads and
preserve their original predecessors. A loopback proxy forwards native Redis
preflight and rejects only the respective typed event append after commitment.
Before recovery, read-only installed Python proves each exact direct fact and
Candidate receipt/head membership, and native Redis inspection confirms its
event has not arrived. Original-key replay retains every SQLite table fingerprint
and exact identities; direct Redis reads then prove event catch-up. All **18**
SQLite tables participate in read/replay checks. The native server configuration
is unchanged. This is controlled relay failure, not server shutdown evidence.

These are metadata-flow fixtures, not completed business work. No Worker is
invoked and no provider request is made. Rejected-audit pending, arbitrary internal
exceptions, current-key revocation, the full claim/terminal fence matrix and
human pending display remain source controls rather than installed-helper claims.

## Retained failures and final packaging

The first regression reproduced four failures for assignment/priority and
generated/explicit keys. Original failed streams remain retained. Earlier
system-Python setup failure and intermediate fixture actor/revision assertion
failures remain separately recorded. Independent review reproduced bare Store
construction errors and requested the finite correction. It also found raw
human-key display and a missing exact rejection-ID assertion; each correction
has a separately bound verification. No failed installed run occurred here.

Independent fixture/native source streams existed without a contemporaneous
combined manifest; a later summary binds those existing bytes and commands and
explicitly records that limitation. One helper parser probe incorrectly expected
clean EOF to raise; its correction is recorded, but the first probe's original
raw stream is unavailable. Later passing controls do not fill that gap.

Archive inspection checks all **217** runtime Python files in both artifacts,
all **193** public Markdown files and public report data in the sdist, privacy
and **81** root guide references. Final governance, package metadata and independent documentation
checks are bound in the closure record; no broader stable claim follows.

Final documentation review found private local paths in historical reports 058
and 059 and benchmark 058 invocation metadata. The earlier archive check used
an incomplete path pattern; its privacy pass did not establish the complete
gate. Original bytes remain retained privately. Public paths are now explicitly
redacted without changing historical measurements or outcomes, and the final
archive check covers broader private-path patterns and exact report-data bytes.
Runtime, tests, installed helper and qualified wheel remain unchanged.

Bulk assignment/priority/retry, boards, persistent dispatch, schedules,
notifications, retention and remaining stable gates remain open. See
[change 066](../changes/066-assignment-priority-durable-pending-results.md) and
[the command reference](../docs/reference/task-board.md).
No commit, remote branch update, tag or package publication is implied.
