# Change 032: Isolate quick-run outputs and claims

Status: Implemented and locally validated.

Quick-goal CLI runs now declare a per-invocation output name. A previously
committed same-name output therefore cannot complete a new quick-run root
before its Worker executes. Each claim selection is restricted to the current
root Contract and Contracts linked below it by immutable parent IDs, including
children published after an earlier loop iteration. The general output
satisfaction rule and domain-wide maintenance behavior remain unchanged.

The shared sample inputs remain content-addressed and reusable. This change
does not isolate maintenance or expired-claim processing across the Store, and
does not change the explicit task-pool behavior of `aig run --once` or
`aig run --task`.

A failed root remains terminal under the existing lifecycle rule. Its recovery
successor is separate work and requires an explicit `aig run --task` call.
Recovery tasks for a failed descendant share that descendant's parent ID and
remain claimable through the quick-run root tree.
