# Continue expanded tasks through ordinary successors

`aig task handoff` now supports an active expanded business obligation whose
available source balance may be zero. One signed transaction cancels the source
and selected live descendants, fences their claims, returns eligible unused
allowance, and publishes one ordinary successor. The successor keeps the original
unfinished outputs and scope; its future plan runs through ordinary tasks.

Traversal closes live grandchildren through terminal intermediaries without
rewriting old terminals. Branches serving a live independent ancestor outside
the transferred root retain their work, holds and budgets. A historically
output-satisfied descendant without a typed terminal receives an explicit signed
completion repair with exact evidence; its residual allowance is extinguished.
Adverse untyped history and open holds on transferred or repair nodes are visible
blockers. Read-only audit and rebuild publish no repair facts.

Expanded successors freeze exact v6 input bindings and the original acceptance
policy. Missing declared input roles block before transfer. Child observations,
results and authorities are not implicitly disclosed. The committed link records
selected nodes, preserved branches, output evidence and funding; exact replay
restores that decision before inspecting later mutable state. Old leaf handoff
identities and replay remain unchanged.

See [ADR-035](../docs/adr/ADR-035-expanded-root-handoff.md) and
[task operations](../docs/reference/task-board.md). Cancellation fences durable
submission, not running OS processes or external side effects. This development
change is part of the continuing Kanban CLI goal; integrated acceptance and stable
qualification require their own evidence.
