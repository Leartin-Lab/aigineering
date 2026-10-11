# Signed task attachment links and exact-byte export

Date: 2026-10-07. Source version: **0.5.13.dev0**.
This records a bounded local file workflow, not stable-release qualification.

## Behavior and boundary

Ordinary `aig artifact import` publishes an artifact-v1 attachment. The new
`aig task attachment add`, `list`, and `export` commands associate its exact
Asset ID with an exact task, inspect safe history, and copy verified original
bytes to an explicit local destination. The existing 8 MiB bound applies.

One feature plugin publishes the signed append-only relation through the shared
Candidate publisher and SQLite commitment fence. There is no new kernel schema,
scheduler or claim owner. Complete historical receipt/head/member checks support
direct and multi-declaration tasks and known handoff/revision/review-request
producers, including exact allowance and relation witnesses. Historical receipts
do not reconstruct the original Candidate payload or re-verify its signature.
Current link authorization and writer revalidation still apply.

Associations do not change Contract inputs, labels, context, tools, allowance,
claims, outputs or policy acceptance. Terminal tasks remain terminal. Listing
omits file bodies and source URIs. Export checks the current Asset against its
immutable publication and verifies length/hash before exclusive-create 0600;
existing files and symlinks are refused. Failed partial copies remain observable
failed files. Explicit Store selection never substitutes a missing default.
Redis is required; accepted SQLite relations survive relay failure and exact
same-key add replay provides recovery after Redis returns.

## Source verification

The full-suite snapshot contains **558** source/config/test/helper files, map
SHA256 `6db7d410784635fdda4b1e2a9a94170524d81ab4f94c8d2797b67954e67f1e41`.
Its **217** runtime files have map SHA256
`6ef0c41fd9047aae9f24daf415695f4c8bccc3191a4cf6c2b1f330f2c5d0da3b`.

- Root serial full suite: **2249 passed**, six existing dependency warnings,
  113.57 seconds, with isolated Stores and real Redis.
- Root Ruff and format checks passed; **478** source/test files checked.
- Author focused verification: **19 passed**, plus Ruff/format and the Node
  effect-wire consumer. Author checks are not independent review.
- Independent final implementation review: **19 passed**, no blocking findings;
  the Node consumer independently checked the one public effect-wire vector.
  These fixture tests mock Redis seams and do not prove native relay behavior.
  Counts overlap the full suite and are not added together.

Source controls cover signed add/replay, explicit-key conflicts, safe pagination,
current-versus-immutable Asset equality, read/export neutrality, partial-write
failure, and claimed/submitted/cancelled/completed state preservation. Actual
signed multi-child declarations, review requests, handoff and revision produce
the tested task heads. A complete forged handoff head referencing an unrelated
terminal fails the specific source-terminal check.

Two-connection preflight-to-writer interleavings cover signed operator
revocation and a changed materialized Asset. The latter is explicit isolated SQL
corruption, not a supported Asset replacement command. Both reject observably
without a relation and preserve claims/status. They do not claim two concurrent
writer executions.

## Retained failures and evidence limits

Earlier author checks against an unintended default Redis endpoint failed; their
original streams were not saved, so only recovered transcripts remain. A fresh
captured check of the existing artifact CLI passed against the explicit isolated
dependency. Later checks do not fill those original-stream gaps.

The writer-test author's first run failed two tests because its wrapper also
intercepted the rejection-audit write. That failed source and original streams
are retained. The corrected wrapper and final integrated run passed. An earlier
direct two-test check also lacks original private streams and is excluded from
the captured final evidence.

The seven-command source CLI checkpoint preserved equal binary export and exact
replay IDs. Its malformed manifest encoding and domain-name/Genesis-ID metadata
errors were corrected through separate retained addenda, without rerunning those
commands or attributing their result to later source bytes.

Earlier unexecuted helper drafts had flag/parsing, workspace setup, rebuild
comparison and pre-retry durability-oracle gaps. Their retained static findings
are not failed installed runs.

## Installed acceptance

The first outside-source attempt installed the wheel successfully, then stopped
after seven successful CLI phases, before the first attachment. Its helper
incorrectly expected a revision envelope from the legacy flat `task status`
response. The original failed streams and packaged helper are retained.

Subsequent helper corrections preserve the flat response and distinguish the
business-completion `ok` field from command success. They compare task readiness,
qualification, claim-related projection hash and integrity health while allowing
only audit-record counters to increase after association. Pure controls reject
missing, malformed or changed projection hashes. These corrections do not change
the runtime or tests covered by the full suite above.

The current helper SHA256 is
`63fc4e7e8e8fa70da56333d146c53080923388f0e91e9fb23c6ad00d899bb8cb`.
Independent static/pure review passed before execution. A fresh outside-source
offline wheel installation then passed **100 command/control phases**, including
expected refusals, against real isolated Redis. No Worker or provider was invoked.
The installed wheel SHA256 is
`1bd94b75ef876a8483d37b458670d9dd56da58558b5fb2f7570c96675725fc91`.
The current source map, including this separately qualified helper, is
`7e037f0e57d7b9fcb826404d46e3f965acb53cb8a0e6868204721423d321da95`;
only the helper differs from the full-suite source map above. Runtime, tests and
configuration remain byte-identical. Earlier full-suite success is not attributed
to execution of the corrected helper.

Installed controls cover ordinary signed import/create/add, exact default and
explicit-key replay, changed-intent refusal, safe fixed-watermark pagination,
foreign/sealed/malformed attachment refusal, unchanged unqualified output and
cancelled task state, custom Store selection, verified binary export, 0600 mode,
existing file/symlink refusal, current-key revocation, reopen and rebuild.
Read/list/export/replay checks compare all **18** original SQLite tables exactly.
Rebuild compares immutable records and semantic materialization digests; it does
not claim unchanged derived display timestamps.

A loopback relay proxy forwarded native Redis preflight traffic and rejected only
the typed attachment relation after SQLite acceptance. The pending result's exact
Candidate and fact identities were proven durable before recovery. Replaying the
same public add through restored relay retained those identities and all table
fingerprints. This is a selective relay fault, not a native Redis server shutdown
or recovery claim. Native Redis configuration was unchanged.

Signed malformed-Asset controls use the installed shared publisher. The full
lifecycle-state matrix, forged histories, materialized corruption and forced
partial-file-write controls remain source evidence; installed execution does not
extend those claims. Independent review retained its own two pure-control setup
failures separately from the passing corrected controls.

Final archive inspection checks all **217** runtime Python files in both archives,
all **191** public Markdown files in the sdist, privacy and root guide references.
Final governance passed **48 tests**, and package metadata checks passed.
The final wheel is byte-identical to the installed wheel above. Independent
documentation review is recorded at closure; this feature does not qualify the
complete stable release.

No new provider-backed business output is qualified here. Attachment unlink,
retention, multi-board lifecycle, remaining bulk operations, dispatch/schedules,
notifications and wider stable gates remain open. See
[the command reference](../docs/reference/task-attachments.md),
[change 065](../changes/065-signed-task-attachment-links.md) and
[ADR-045](../docs/adr/ADR-045-signed-task-attachment-links.md).
No commit, remote branch update, tag or package publication is implied.
