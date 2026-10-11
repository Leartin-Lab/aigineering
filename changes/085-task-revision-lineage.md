# Navigate accepted review-linked task revisions

`task lineage ID` now follows a verified `revision_publication` between the
exact source and successor of an accepted `task.revised` publication. The
reader uses the same immutable task-view snapshot and indexed receipt,
committed-head, member and exact Contract-declaration proofs as handoff
navigation. It requires the sole `task.revise` effect, the successor declaration
and exactly one cancelled source terminal in the revision head, with both
directly linked to the `task.revised` record. Starting from either endpoint
returns the same component and each Contract keeps its own projected status and
blockers.

Handoff and revision use a shared pure successor-publication proof; their
wrappers retain their fixed effect types and public issue names. An unverified
relation remains a safe issue and cannot expand the component. A repeated
source/target successor relation removes all competing publication edges and
reports the corresponding feature-specific ambiguity. Duplicate materialized
Contract IDs retain the existing query-level integrity failure.

Revision lineage establishes the accepted publication relation only. It does
not re-audit the historical review decision, report, evidence, allowance or
assignment decision, and it does not evaluate current eligibility. The existing
revision publisher and replay behavior are unchanged. No Worker or provider is
invoked by lineage inspection. See [DESIGN](../DESIGN.md), the [task board
reference](../docs/reference/task-board.md), and the [Aigineering
Skill](../SKILL.md).
