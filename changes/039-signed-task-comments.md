# Signed task comments

Date: 2026-10-04
Source version: 0.5.13.dev0; development only, no release tag

`aig task comment` appends a signed, task-scoped statement; `task comments`
reads the durable conversation by revision. Replies reference committed comments
on the same Contract. Attribution comes from the authenticated actor and key.
The exact payload and 65536-byte UTF-8 limit are checked before projection.

The pure collaboration plugin owns the new `task.comment` effect and
`task.commented` fact. The existing Candidate commitment path owns writes. The
application injects the plugin registry explicitly; the core does not import
the plugin and additional handlers cannot replace built-ins.
The CLI provisions a separately authorized collaboration actor; Worker submission
authority does not imply comment authority. Comments do not satisfy business
outputs or change task status, claims, allowance, or acceptance. Terminal tasks
remain commentable.

Repeated identical input with the same key returns the stored record, including
its original timestamp and revision. A changed input is a different Candidate.
Redis remains required; failures after commitment retain facts and expose sync
pending. Public vectors are consumed by Python and an independent Node verifier
for signing bytes, signature, Candidate identity, and RuntimeRecord identity.

This slice does not implement manual holds, assignment, priority, review
requests, multi-board administration, or a dispatcher. Those remain part of the
continuing CLI goal.

Decision: [ADR-028](../docs/adr/ADR-028-signed-task-comments.md).
Usage: [task operations](../docs/reference/task-board.md).
