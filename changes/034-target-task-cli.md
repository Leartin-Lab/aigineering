# Change 034: Scope target runs and report actionable outcomes

`aig run --task CONTRACT_ID` now claims only that Contract and Contracts linked
below it by immutable `parent_id` references. Capability-routed tool Workers
retain their required-contract-capability filter. The command checks the target
projection before maintenance or Worker execution, so an already terminal
target causes no claim attempt. `--once` keeps its pool behavior, and target
execution continues to use domain-wide runtime maintenance.

When the target run stops, JSON and human output preserve the projected task
status and blockers and add a `stop_reason` (`terminal`, `idle`, `timeout`, or
`worker_failure`) plus an operator `next_step`. A durable Worker invocation
failure includes its stable recorded failure categories and known recovery
Contract IDs. Recovery successors remain explicit follow-up work; the command
does not claim a root recovery successor automatically.

Recovery IDs in task status, active-recovery risk checks, descendant failure
risks, and target-run invocation failures are attributed through exact durable
relations: recovery scheduling trace targets, recovery context references, or
an explicit recovery parent ID. Contract-name prefixes are not used, so
same-name roots do not inherit one another's recovery guidance.

Target-specific CLI regressions cover unrelated ready work, a terminal target,
ordinary and recovery descendants, tool-capability filtering, blocked idle
output, and timeout guidance. CLI wait and interval numeric validation is
provided at the command boundary.
