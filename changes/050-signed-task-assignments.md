# Signed task assignments

Status: Implemented in the 2026-10-05 development tree; deterministic and installed verification passed, actual-output acceptance remains open.

`task assign`, `task reassign` and `task assignments` provide initial Worker
selection, exact-head replacement/clear and immutable history. Each mutation
publishes a signed Candidate with assignment authority. The current head remains
observable after clear. Busy compatible Workers can receive assignments; actual
claims still obey capacity and current identity checks.

The shared pure assignment owner and SQLite writer enforce the selected Worker
across scan and direct claim paths. Any claim history forbids in-place changes.
Assignment does not modify Contract scope or output acceptance. Handoff and
review-linked revision inherit the restriction in the successor transaction;
explicit set/clear carries exact signed source-head intent and additional
assignment authority. Default no-history successors preserve their old identities.
Exact Candidate replay returns the original fact before mutable state checks.

Task status and board cards expose assignment and blockers. Redis schema-v2
events include typed Worker IDs and support scoped rebuild; event fields are
derived metadata, never claim authority. The reader floor advances to schema21.
Stop old Workers and back up with `domain upgrade`; unsupported pre-21 assignment
imports reject. Already-open old connections are outside the reopen guarantee.

See [ADR-038](../docs/adr/ADR-038-transactional-task-assignments.md), the
[operator workflow](../docs/reference/task-board.md#assign-exact-work) and the
[local profile promotion boundary](051-local-worker-profile-promotion.md).
Registration and signed assignment do not establish accepted model output.
