# Immutable task edit development verification

Date: 2026-10-06
Version: 0.5.13.dev0
Status: Bounded development verification complete; stable qualification remains open.

## Verified behavior

`task edit` replaces title, body or both through the existing signed handoff
owner and returns a new successor ID. Omitted text is unchanged; an empty body
is supported. The reason stays in audit metadata. Canonical NFC no-ops,
malformed text/shape and stale assignment/hold snapshots reject. Existing
handoff payloads and the twelve predecessor wire vector files remain unchanged.

The old attempt is cancelled and fenced. Remaining outputs, allowance, frozen
inputs/disclosure, routing and independent acceptance retain their existing
owners. Signed expanded editing preserves the live external verifier branch
and inherits the exact source assignment. Three additional tests compare every
serialized non-text Contract field with distinctive scope, routing, v6 binding
and independent-policy values, allowing only expected successor differences.

The complete source hold set, up to 64, is atomically copied with exact reasons
and provenance. Old placements remain historical; descendant holds still block.
An actual signed SQLite commit verifies the 64-copy boundary. Explicit edit
authority is required, and copying holds additionally requires block authority.
Missing authority and revoked exact replay cannot create another successor or
regrant a local identity. Exact replay survives old-source hold cleanup and
preserves later successor holds. Accepted relay failures retain facts and return
the durable successor through a structured pending result.

## Exact snapshot and checks

The working tree remained uncommitted at base HEAD
`01be3f4e585acd7975284a38e88dbdcbb44724c0`. The accepted runtime inventory is
207 Python files, SHA-256
`9f00ac884c1b1e3af6c270edfce6903bd1843aeb11e2af5428d123c4d4afc229`.
The full source/test/script/vector inventory is 524 files, SHA-256
`f68b77c46cbf31dd2b06be79fb4b9d9efe8fd9def6005a60f38cadf9f0749706`.
Before and after the integrated suite these maps were identical. Later public
documentation updates do not replace the source evidence.

| Evidence | Result and scope |
| --- | --- |
| Integrated serial `pytest -q` | 2105 passed, 6 dependency deprecation warnings, 108.94 seconds; no skips |
| Ruff check and format | Passed for source/tests; 451 files already formatted |
| Independent source2 review | 73 focused edit/handoff/expanded/race/conformance/architecture tests passed; no implementation blocker |
| Independent source3 delta | 3 complete-field comparison cases passed; production files unchanged |
| Independent final review | Six delivery documents reviewed; no scope blocker. Installed wheel with source-imported helper passes against real Redis in a separate domain |
| Cross-language vectors | All 13 pass their appropriate Node consumers; twelve predecessor files unchanged |
| Build and Twine | Wheel and sdist built successfully; metadata checks pass |
| Artifact inventory/privacy | Complete runtime and then-current 167 public Markdown files match; new helper/vector/consumer included; no credentials, private workspace references or raw stores |
| Root Agent guide | All 64 direct references resolve |
| Outside-source installed wheel | Offline installation and packaged-sdist helper passed against real Redis; imported package is outside the checkout |

Independent subsets and author subsets overlap the integrated suite and are
not additive. The new Node consumer independently checks signing bytes,
Candidate identity/signature, receipt and projected fact identities, exact
declaration/text/link references and ordered causal parents. Tampering a link
parent fails identity validation. This is wire evidence, not a second runtime.

Six forced two-connection tests pause between projection/preflight and writer
entry. Source hold placement/release, assignment-head rotation and a competing
edit durably reject the stale edit without partial transfer. A claim committed
first is closed by edit; an edit committed first fences the stale claim. Those
claim races use trusted SQLite claim seams and do not prove an authenticated
WorkerHost command race. Actual installed signed `worker next` is separate
sequential command evidence.

## Actual Redis and installed control

The installed helper publishes a signed edit, verifies copied and later holds,
reads one immutable history entry and replays after old-source hold cleanup.
It observes 21 Redis events before successor claim. At that phase, reopen and
rebuild preserve immutable IDs and semantic digest. It then explicitly releases
both successor holds and successfully claims the new task through signed CLI
`worker next`; the cancelled source has no new claim. Rebuild-before-claim is
not represented as rebuilding that later active claim.

Verification used explicitly configured isolated Redis 7.4.11 with private AOF
persistence. Its official source archive matched the
[published Redis checksum](https://github.com/redis/redis-hashes/blob/master/README)
and was built using the
[versioned build instructions](https://github.com/redis/redis/blob/7.4.11/README.md).
This dependency is separate from the unresponsive original container endpoint;
these passing checks do not establish that original service's recovery.

The first built wheel is SHA-256
`a1f31d1b80fb44f8038cc7d3197ff65a70a83c9cc370ed7115dd3bab1bffdc79`.
Its runtime bytes equal the final source3 runtime; the intervening change added
tests only. The installed result manifest is SHA-256
`230a5242d48289e4ce7e2153b7d31f7c771396aa8a8e81709d67a965296c6292`.
The integrated suite log is SHA-256
`21621d4ab048fa8fd3f57a1b1827ea8fba27ea8f5320dd67a3c1de4d7c3d95d4`.

## Retained failures and limits

The first integrated suite had 52 failures and 2050 passes in 200.89 seconds,
with unchanged source2 maps. Redis socket timeouts, pending relay and domain/
worker service-unavailable failures were retained. Independent PING timed out,
and Docker inspection timed out while the host service reported running. A
targeted container restart request timed out with unknown outcome. Successful
verification on a new isolated real instance does not erase those failures.

An initial installed launch was not executed because automatic approval review
disconnected before completion; that transport failure was explicitly not an
unsafe-action determination. Independent source helper attempts retained an
import-path failure, sandbox denial and an escalated Redis-unavailable failure.
A follow-up independent native-helper turn failed before execution because the
selected model was at capacity. On 2026-10-07 a separate independent review
passed with an installed-wheel executable and source-imported helper. Its
sandbox attempt first failed at domain initialization; formal escalation proved
PING health. An escalated retry reused its existing helper directory and stopped
with FileExistsError before CLI invocation. A fresh directory then passed.
These attempts are retained separately from root packaged-artifact controls.

Author failures included invalid fixture setup, a replay assertion wrongly
requiring the handoff link to be the final member despite allowance coeffects,
a history renderer expecting an unavailable key, and an accidentally omitted
legacy post-commit sync call. Product defects were repaired and legacy behavior
rechecked. Test oracles for authority, enum casing, copy-ID ordering and expanded
budget were corrected. Earlier author pytest outputs were preserved in tool
history and isolated fixture directories, with missing raw redirected logs
explicitly recorded. Root's first vector invocation used the wrong consumer;
that TypeError was retained and the documented command corrected.

These controls made zero provider calls and produced zero business outputs.
They do not establish model quality, operating-system isolation, exactly-once
external effects or stable qualification. Independent final document review
found no scope blocker. The updated delivery inventory is checked separately
from the first build above. The wider Kanban stages and current-provider
actual-output gates remain open.
No commit, push, tag or package publication occurred. See
[the change](../changes/055-text-edit-through-shared-handoff.md),
[ADR-043](../docs/adr/ADR-043-text-edit-through-shared-handoff.md) and
[the operator workflow](../docs/reference/task-board.md#edit-task-instructions).
