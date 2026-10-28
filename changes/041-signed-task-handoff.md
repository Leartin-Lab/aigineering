# Active task handoff through bounded successors

Date: 2026-10-04
Source version: 0.5.13.dev0; development only, no release tag
Status: Completed development slice; stable qualification remains open

The task CLI adds `handoff` and `handoffs`. A signed operation cancels the old
attempt and publishes a successor with bounded allowance in one transaction.
The old claim is released and late submission is fenced. Optional successor
holds allow an operator to resume through the existing exact `task unblock`.

Already completed outputs are retained as auditable metadata; the successor
executes only the remaining slots and preserves the predecessor's frozen scope
and acceptance policy. Exact keyed replay preserves the original link, budget
and target. This slice does not transfer an expanded task tree or implement
review and change requests.

Injected effect decisions are revalidated with current facts inside the writer
transaction. Changed decisions produce a durable rejection before writes;
feature semantics remain outside the core. Current task projections also read
Candidate-published terminal facts directly, fixing cancellation views that
previously depended on a terminal trace.

Identity revocation during projection revalidation now leaves durable
authentication rejection without resubmitting an invalid receipt. Polling a
late rejection for non-failed terminal work preserves its existing outcome and
records unavailable recovery; failed-task recovery retains its existing path.
Handoff metadata explicitly references both source and successor in task-scoped
events, without extracting task references from operator text.



Decision: [ADR-030](../docs/adr/ADR-030-transactional-task-handoff.md).
