# Bulk task lifecycle development verification

Date: 2026-10-07
Version: 0.5.13.dev0
Status: Bounded development verification complete; stable qualification remains open.

## Verified behavior

`task block`, `unblock` and `cancel` accept 1–64 distinct task IDs. Each target
publishes its own signed Candidate through the existing application owner and
SQLite transaction. Results retain partial successes and failures in input order;
there is no all-task transaction or batch-owned scheduler. Exact `--hold-for
TASK=HOLD` maps support unblock and the original complete per-task hold sets
for promotion replay. Existing single-target keys and promotion derivation are
preserved. Multi-target actual keys are stable across reordering and subsets;
replaying one member uses that member's returned actual key.

Global shape/map and required Redis preflight precede target publication.
Post-commit relay outages retain accepted facts and return their exact durable
Candidate/fact identities. Rejected audit relay, accepted pending, unknown and
not-attempted outcomes remain distinct. Unexpected errors stop later targets
without misclassifying prior accepted results. Released holds are not recreated;
later holds survive original promotion replay; cancellation preserves claim fences.

The GPT-6 Luna author supplied 51 passing focused checks. Root independently
reviewed all 14 implementation/test/change files at their frozen hashes with no
blocker. The helper was authored separately; author checks are not independent
acceptance. A separate Luna document review found the roadmap's implementation
status stale; root reconciled it with the actual integrated gates.

## Exact source and integrated verification

The uncommitted working tree is based on HEAD
`01be3f4e585acd7975284a38e88dbdcbb44724c0`. The complete source/config/test/
script/vector inventory contains 530 files, SHA-256
`87e21254d4a866255177feda418062cc81511828067cf4463199640ac262c31e`.
The runtime inventory contains 208 Python files, SHA-256
`9df6c6892280ce7f9573d768a4eaf5f6029527c8bc22a005cb01ee0b53bef70b`.
The maps match before and after the integrated run. Public documentation is
checked separately from this source evidence.

The serial integrated suite passed **2128 tests**, with 6 dependency deprecation
warnings, in 109.45 seconds. Ruff check and format passed for source/tests;
456 files were already formatted. All 13 Node vector consumer invocations
passed; their fixture and consumer hashes are unchanged across the two source
snapshots. Author and independent subsets overlap this suite and are not additive.
The integrated stdout SHA-256 is
`a45e8164b79ebae42918450befdf6435d8925ba7a56c147bf77ed7b2468d0b92`.

The first integrated run retained 2 failures and 2126 passes: the commitment
coordinator exceeded its existing size constraint, and an old missing-key test
expected an unauthenticated submission to consume a claim. Delegation to the
existing rejection owner restored the 299-line coordinator without relaxing the
gate, and the obsolete oracle was corrected. The second run above validates
the resulting source; the first failure logs remain retained.

## Source CLI control and retained failures

The helper's final source-CLI control completed 93 commands against real Redis
7.4.11 with an explicitly configured isolated domain. It verified partial
results, exact replay, later hold preservation, signed Worker claim fencing,
11 accepted bulk facts on the Redis feed, and all 139 record IDs/timestamps and
the semantic digest across reopen/rebuild. This is source execution, not an
installed-package claim. The summary SHA-256 is
`a011bd39e511a5155faf4f3cd0fc13506851cdd0dd7a77a634b25a5fd82e5416`.

Three earlier helper runs are retained separately: sandbox Redis denial at domain
initialization; a partial-result parser wrongly requiring all-success; and an
object-only parser rejecting the correct no-work JSON null. The latter two were
helper oracle defects. Corrected parsing preserves failure and null semantics;
no operational fallback was added. Real Redis verification uses a separate
isolated persistent instance and does not establish recovery of the previously
unresponsive container endpoint.

## Artifact acceptance

The first wheel and sdist built successfully and passed Twine metadata checks.
Their complete 208-file runtime inventory matches the tested source. All 172
then-current public Markdown files match the sdist, the root guide's 68 direct
references resolve, and archive privacy checks pass. Final delivery documents
are checked separately from that first archive.

Root installed that wheel offline in a fresh environment outside the checkout,
removed PYTHONPATH, and imported the bulk helper extracted from the built sdist.
Its 93-command control passed against real Redis: all 11 accepted bulk facts
appear on the event feed, and all 139 record IDs, timestamps and the semantic
digest survive reopen/rebuild. This is actual signed CLI control, with zero
provider requests and no business output. The wheel SHA-256 is
`94223e1ae6b51eb70adb2d448ff40fa5ee8a92b49a336211e786efe38453af79`.
The packaged-bulk stdout SHA-256 is
`542147109500f1b0173e6d12715b3c2cb2148c1f887f4dba8b1794447f38d3cb`.

The combined first installed invocation later exited 1 during the separate
original authentication contrast probe. It configured one Worker slot; the
correctly retained active claim prevented its second contrast claim. That
fixture failure does not erase the completed bulk phase and is retained in the
combined manifest. The adapted authentication check is reported in
[report 088](088-authentication-rejection-isolation-2026-10-07.md).

## Limits

These controls make zero provider calls and produce zero business outputs. Bulk
assignment/priority/retry, attachments, actual run logs, multiple boards,
persistent dispatch, scheduling, notifications and retention are outside this
slice. Current-provider actual output and wider stable qualification remain open.
No commit, push, tag or publication occurred. See
[the change](../changes/056-bulk-task-lifecycle.md) and
[the operator guide](../docs/reference/task-board.md).
