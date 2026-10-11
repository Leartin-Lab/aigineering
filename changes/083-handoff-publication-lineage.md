# Accepted handoff relations in task lineage

`task lineage ID [--json]` now traverses verified accepted task handoffs for
leaf continuation, expanded handoff, and text-edit handoff. The handoff owner
proves one exact `task.handed_off` member, its accepted sole `task.handoff`
receipt and committed head, the source cancellation terminal, and the successor
declaration in that same head. It also proves exact source and successor
declaration publications and requires each declaration payload to match its
materialized Contract. A source publication may include other signed effects.

The same edge is returned from either endpoint with source declaration,
receipt, and head; handoff link, receipt, and head; cancellation terminal; and
successor declaration, receipt, and head IDs. Invalid or ambiguous link
evidence produces a safe issue for whichever endpoint is known and does not
traverse the handoff relation. The view reuses one captured task snapshot and
the shared generic publication-history index.

The relation is an accepted historical assertion that links two exact
published Contracts. It does not independently re-audit the handoff's
historical budget, descendant tree, holds, assignment, output qualification,
or allowance decision. Revision, tool request/observation/continuation, and
other recovery families remain outside the verified lineage view.
