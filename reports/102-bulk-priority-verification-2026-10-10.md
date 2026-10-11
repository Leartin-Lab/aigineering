# Bulk priority missing-target development verification

Status: bounded development verification; stable qualification remains open.

Priority now reports a genuinely absent task as `not_found`, allowing later
members of a bulk request to run. Exact orphan declaration or priority history,
ambiguous materialization and malformed history remain integrity failures that
stop later attempts. Both validation and publication use the shared application
guard. See [change 081](../changes/081-bulk-priority-missing-target.md).

The uncommitted `0.5.13.dev0` snapshot over
`01be3f4e585acd7975284a38e88dbdcbb44724c0` is bound by the exact runtime module
map and captured check hashes in [verification data](data/102-bulk-priority-verification.json).
Independent focused review passed 41 tests. Root full verification passed 2523
tests with six dependency warnings; Ruff, format, build and Twine passed.
All 228 runtime modules matched source, wheel, sdist and the source-external
installation. Artifact inspection found no private files, credentials or stale
public documents at the tested build snapshot. Third-party dependencies were
shared without editable path files; a clean dependency-index installation was
not tested.

## Installed operational evidence

A fresh signed metadata fixture used the installed console and native Redis
7.4.11 for 18 captured phases. Priority returned
`redis_sync_pending / not_found / accepted`; retry returned
`not_found / redis_sync_pending / accepted`. Each exact replay retained the
accepted Candidate, fact and operation key, with unchanged runtime-record
inventory and revision. Two controlled relay proxies each intercepted their
one expected event, then joined and refused connections on their closed ports.
Mixed batches retained exit 1 because a target was still not found.

The earlier failed priority request from [report 101](101-verified-task-lineage-2026-10-10.md)
was separately recovered on its original Store with its original arguments and
keys. The first accepted-pending fact replayed unchanged; the missing target
returned `not_found`; the previously unattempted tail was accepted. Revision
29 advanced to 33 through exactly four tail publication records: receipt,
priority fact, committed head and canonical decision trace. All preexisting
runtime-record value hashes remained unchanged. A subsequent one-shot replay
retained every exact result and revision 33; before/after read-only proofs
confirmed complete runtime-record equality. This does not establish equality
of every SQLite table or separate trace storage.

The first recovery checker stopped after the correct CLI result because it
required a pending-only field on ordinary accepted rows. That failed checker
and the original failed run remain retained. A corrected read-only checker
validated the already committed tail without repeating the first recovery
write. The later exact replay was captured separately. The original run's
retry phase remains unattempted; the fresh retry fixture above is separate.

## Remaining qualification

These checks invoked no Worker or provider and produced no business output.
Current useful DeepSeek Flash execution, independent acceptance/export,
remaining lineage families, complex planning and whole-lifecycle release gates
remain open. No commit, push, tag or package publication was performed.
