# Task assignment development verification

Date: 2026-10-05. Source version: **0.5.13.dev0**. This records development
verification of [signed task assignments](../changes/050-signed-task-assignments.md),
following [authenticated Worker coordination](081-authenticated-worker-coordination-2026-10-05.md).
Deterministic and installed checks pass. Actual assigned model work failed
authentication and produced no report. This does not close that live gate,
qualify a stable release or complete the continuing Kanban CLI objective.

## Behavior and boundary

`task assign`, `task reassign` and `task assignments` manage one exact Contract's
signed assignment history. A clear remains a real head; changes require its
exact predecessor. New in-place changes reject any claim history. Wrong Workers
cannot claim through either signed application commands or the trusted Store
API. Current registration, capability, pool and capacity still govern claims.
Busy Workers can receive queued assignments; placement is not execution.

Signed handoff and revision inherit an assigned source atomically, or explicitly
set/clear the successor through a separately authorized decision. Ordinary
children do not inherit. Assignment cannot widen inputs, tools, allowance or
output qualification. The writer rechecks frozen handler ownership, assignment
intents, typed witnesses, current keys and current facts before any durable
write. Exact authenticated replay returns its original decision after later
head changes or terminal outcomes.

Schema21 blocks normal reopen by older readers before the first assignment.
Genuine schema20 records retain their identity; unsupported pre-21 assignment
imports refuse migration. Stop old Workers and retain the upgrade backup;
the marker cannot revoke already-open old connections. Redis is required
collaboration infrastructure. Its typed event Worker references may include
historical assignees and have no claim authority. See
[ADR-038](../docs/adr/ADR-038-transactional-task-assignments.md) and the
[operator guide](../docs/reference/task-board.md#assign-exact-work).

## Exact deterministic and independent evidence

The final 479-file code/configuration/vector/test map SHA-256 is
`8b6d972def3993eb0a574ec43548e9a91c45cb66aa79bf209f795168f329d7cb`.
Its 202-file runtime map is
`b157fd1f680209a174b53338e83f485833a15e36af261db1a2e40d1c161d16ed`.
Ruff check and format pass. Serial full3 passed **1,955 tests**, with six
warnings and no skips, in 79.67 seconds; log SHA-256
`1543ade6b83109ed08e0c89115c445110a93b1d9ae5c466f52c6966d3ad72c46`.

Forced barriers cover assignment-first/claim-first arbitration, competing heads
from distinct Store connections, key/registration changes after preflight and
all historical claim statuses. Signed HTTP claims preserve the same assignment
boundary. Outputless Contracts remain assignable; nonempty satisfied output
slots prevent new placement. Legacy handoff/revision conformance vectors remain
unchanged; typed handler consumers and the new assignment vectors pass.

Independent GPT-6 Luna review of source3 passed 31 focused routing, ownership,
conformance, event and handler-copy checks. Source4 changes only a Redis test
fixture, with no runtime changes. Independent review checked that exact delta
and passed 10 Redis collaboration cases, or 16 including Redis CLI cases;
manifest SHA-256
`ba23bb7b5152b2076f410afc21968bf08ea9c55a923d3bddbe3c24dddecbca2a`.
These overlap other focused checks and are not one independent full suite.
The earlier review missed core plugin fact names; the correction is retained.

The routing efficiency test stubs the existing global expiration maintenance
to isolate additional projection reads. That maintenance still scans broadly
before routing. This evidence proves removal of a local read regression, not
an end-to-end no-scan or performance claim.

## Installed and genuine predecessor evidence

Build3 wheel SHA-256:
`21344336f573f6fcac90227c978d43bd10fe3ed0bb6630586f2cd30bc806d853`.
All 202 runtime module bytes match the reviewed source. The complete installed
CLI runner passed outside the source tree, with fresh domains, real Redis and
explicit signed fixture outputs; provider calls: **0**. Its main reconstruction
preserves 536 records under schema21; manifest SHA-256
`9d250786ab6abd9e926757cba413771ac886909389c9259ed9c81851b7e600c7`.

The separate assignment fixture checks set/replace/clear/set, denied signed and
trusted wrong-Worker claims, claimed-source handoff, explicit successor clear,
first assignment through a no-history override, terminal replay and eight exact
assignment facts with receipt/declaration/registration/predecessor/link
provenance. SQLite and scoped Redis rebuild preserve facts and views;
assignment fixture manifest SHA-256
`8c30c2ed5a149bb9496c1fed898d6eb1009562ebffc5bbe3be29b9cc9837adfd`.
Fixture completion is not real-provider output or independent qualification.

A genuine schema20 runtime from the preceding accepted SDIST created nine
records including a released claim. The installed build3 runtime upgraded with
a mode0600 backup, preserved record IDs, claim identity/status and materialized
digest through rebuild, and treated upgrade rerun as a no-op. Both genuine
schema20 operational and snapshot readers refused21. Reader manifest SHA-256
`7b50e3c7049c54d43980c1d6e9f38450715b5df6b75beb9cda27e4700da56088`.

## Retained failures and actual work limitation

Full1 retained nine failures: two real core ownership/read regressions and seven
stale typed-handler/event consumers. Conformance failed before projection; no
golden bytes were changed to fix it. Full2 retained five failures from incomplete
Redis test records lacking mandatory causal parents. Source4 supplies that field.
Earlier installed runners retained their null-package handling and nonexistent
event-option failures. Earlier predecessor probes retained incorrect probe
API/reader/exception assumptions. Later passes do not erase those attempts.

An installed ordinary task used a frozen exact public-document Asset, allowance1,
no tools and the requested `deepseek-flash` model. Other Workers returned no
package before and after exact-head reassignment. First startup stopped before
model invocation because `worker next` had registered a pull-only profile and
the same ID/version could not be reused with a different model profile. This
entry-point registration conflict is confirmed unresolved technical debt.

A separately registered real model Worker received the task through signed
reassignment before its first claim. One provider invocation, with retries
configured to zero, returned **HTTP401**. The assigned Worker claim was released;
the source task became visibly failed and a recovery successor was published.
That successor was not executed. There is no report output, canonical independent
acceptance or successful assigned provider result. Token usage and money are
unknown, not zero. Actual-failure manifest SHA-256:
`56d1c4649e909f7ee14c58e280d6bf20a0224f4812a6d356c8c3f8ff41258c2a`.
The credential's API endpoint must be confirmed before another invocation.

Priority, bulk operations and the later dispatch/retention/attachment capabilities
remain separate work. These are local development edits and verification;
no commit, remote update, tag or package publication is implied.
