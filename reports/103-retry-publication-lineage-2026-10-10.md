# Retry-publication lineage development verification

Status: bounded development verification; stable qualification remains open.

`aig task lineage ID` now navigates canonical signed retry publications in
both directions. Each edge binds the exact source and target declarations,
receipts and committed heads, the target Candidate's source parent and the
complete canonical retry Contract. Tasks retain their individual status and
blockers. Unverified evidence cannot expand the component. See
[change 082](../changes/082-retry-publication-lineage.md).

The uncommitted `0.5.13.dev0` snapshot over
`01be3f4e585acd7975284a38e88dbdcbb44724c0` is bound by
[verification data](data/103-retry-publication-lineage-verification.json).
Independent focused verification passed 58 tests. Root full verification passed
2543 tests with six dependency warnings; Ruff and format checks passed. Build,
Twine and source-external installation passed. All 228 runtime modules matched
source, wheel, sdist and the installed package. Third-party packages were shared
without editable path files; a clean dependency-index installation was not tested.

## Installed controls and preserved failures

The installed console and native Redis 7.4.11 published ordinary signed tasks
A→B→C through the existing create/retry owners. JSON and human lineage queries
from all three tasks returned the same component and two exact retry edges.
Exact retries retained complete result `data`, Candidate/fact/key identities
and runtime-record inventories. The top-level observed revision can advance
after other work; it is not the original publication revision. A Redis outage
remained a visible `redis_unavailable` error.

The original helper completed 26 command phases with their expected exits, then
failed because its checker compared canonical lineage task fields with the
additional CLI `integrity` envelope. A separate read-only continuation initially
failed on a tuple/list JSON comparison. Both failed runs remain failed and retained.
The corrected continuation passed five phases on the original Store: exact
artifact binding, canonical task/status comparison and reopen, read-only SQLite
backup, private-copy rebuild, and a final original-Store check. Full CLI status
envelopes, including integrity results, remained equal. Original revision 16
and its 16-record inventory stayed unchanged; the private-copy semantic digest
and complete lineage data remained equal across rebuild.

The first full-suite run had a misconfigured Redis endpoint and restricted
local network access; its failures remain retained. The next run exposed one
test that stopped at the shared CLI preflight instead of testing the lineage
callback. An isolated registered-command test now checks that callback, closes
its snapshot, preserves the stable error code and avoids fallback or private
detail disclosure. Foreign-index tests likewise require zero witnesses and safe
issues; generic index provenance rejection remains with the shared proof owner.

## Remaining qualification

These installed controls invoked no Worker or provider and produced no business
output. Compound-source retry behavior has signed source-fixture evidence;
the installed fixture used a single-declaration source. A handoff-successor
retry fixture was rejected with zero available allowance; no budget semantics
were changed to admit it.

Handoff, revision, tool request/observation/continuation and other recovery
families remain outside this verified view. Current useful DeepSeek Flash
execution, independent acceptance/export, complex planning, hundreds-node
management and whole-lifecycle release gates remain open. No commit, push,
tag or package publication was performed.
