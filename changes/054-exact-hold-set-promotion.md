# Atomic recovery of manually held tasks

Status: Bounded development implementation verified; stable qualification open.

`task promote ID... --reason TEXT` releases each task's complete observed manual
hold set through one signed Candidate. `task promotions ID` reads its attributed
audit history. Results report exact hold/release IDs, the idempotency key,
observed task status and remaining blockers. They do not promise readiness.

The collaboration plugin uses existing `task.unblock` authority. It emits
ordinary hold releases and one exclusively owned audit. The generic hold owner
allows up to 64 same-kind, same-task transitions and retains its authenticated
receipt/head and current-history checks. Writer-time reprojection rejects a
changed complete set atomically. Existing block/unblock wire behavior is retained.

Multiple task IDs are independently atomic and report partial failures with a
nonzero exit. An explicit original `--hold` list, reason and returned key permits
exact replay without releasing later holds. Empty, terminal, previously claimed
or output-satisfied fresh targets reject. Dependencies, activation, assignment,
capacity, claims, allowance and acceptance are unchanged. Redis remains required;
pending relay preserves accepted facts.

No Contract identity, Store schema or reader-floor change. Promotion does not
edit task text or reset claims. See
[ADR-042](../docs/adr/ADR-042-exact-hold-set-promotion.md) and the
[operator workflow](../docs/reference/task-board.md#recover-manually-held-work).
