# Signed task execution dependencies

Status: Implemented; runtime development verification completed.

## Problem

Operators could hold a task manually, but could not require successful completion
of an exact prerequisite through the ordinary claim boundary.

## Resulting design

`aig task link TARGET --depends-on PREREQUISITE --reason TEXT`,
`task unlink TARGET --link RECORD_ID --reason TEXT`, and `task dependencies TARGET`
share one application publisher and immutable fact history. A single authorized
Candidate places/releases a generic claim constraint. One core owner validates
edges, cycles, provenance and typed successful terminals; SQLite rechecks this
state while committing control changes and acquiring claims. Task views and
Redis events expose the exact endpoints and blockers.

Links do not change frozen Contracts or supply input Assets. Completion still
uses canonical output/policy semantics. Cancelled prerequisites remain exact
blockers; safe replacement uses hold, unlink, link and exact hold release. Signed
handoff/revision guards outgoing external prerequisites without cloning edges or
rewriting downstream IDs. See [ADR-036](../docs/adr/ADR-036-task-prerequisite-claim-constraints.md)
and the [operator guide](../docs/reference/task-board.md).

## Public compatibility

Schema20 prevents normal reopen by readers that ignore dependency restrictions.
Upgrade retains earlier records and refuses unsupported pre-20 constraint rows.
Stop old Workers; already-open old connections are outside this gate. No task
identity, input-binding format or allowance-return semantics change. Workers do
not gain the operator dependency capability by default.

## Required verification

Signed positive/negative intent, replay, output-completion independence, HTTP and
direct claim parity, two-connection writer races/cycles, historical projections,
old-reader migration/refusal, Redis outage/rebuild, language-neutral wire vectors,
installed CLI and bounded real work. Full lint, format, tests, build and artifact/
document checks bind the eventual development closure to a frozen snapshot.

## Closure

Core and combined independent review, 1,903 integrated tests, actual Redis
relay/rebuild, outside-source installed runtime and two bounded real provider
tasks passed. Preparation and installed-process failures remain retained; a
separate editorial guide is not canonical independent acceptance. See
[development report080](../reports/080-task-execution-dependencies-2026-10-04.md). This change does not qualify a stable release or finish the broader
Kanban CLI goal. No tag or publication is implied.
