# Named local boards

Status: Implemented with bounded development verification.

## Problem

Directory selection and a one-domain task board did not provide a persistent
named workspace lifecycle. Multiple projects needed explicit routing without
adding mutable task state or another scheduler.

## Resulting design

`aig board create/list/show/switch/archive` manage a project-local registry.
Managed creation initializes `.aig/boards/NAME`; `create NAME --workspace PATH`
adopts an initialized workspace. Entries pin canonical paths and exact Genesis
IDs. Create does not switch selection. `archive NAME --restore` restores the
metadata flag. Archive keeps a selected pointer, so implicit operations fail
until explicit switch or restore.

Saved selection routes ordinary commands. `aig --board NAME ...` temporarily
selects one entry; explicit `-C/--workspace` bypasses the registry and is mutually
exclusive with `--board`. Management stays at the registry root. Missing,
corrupt, unsupported, replaced or archived selections never silently fall back.
Human output and `--json` are available for management. Domain availability in
list/show is not task readiness or Redis health.

`local_domain.py` shares explicit bootstrap with `domain init`. Registry updates
use a bounded private schema, serialized writers and atomic replacement.
Post-Genesis relay or registry failure preserves the domain and reports setup
pending; retry adopts that same domain. If the entry is visible after a failed
directory durability confirmation, `create NAME --resume` verifies the complete
entry/pin and republishes unchanged metadata under the lock, preserving
selection/archive. Standard duplicate create remains refused. A partial root key without Genesis
requires explicit recovery. Existing signed task publication, claims, views and
Redis domain namespaces continue through their original services.

## Public compatibility

Without a registry/selection, the original cwd behavior remains. Explicit
workspace access still works. Relative command inputs resolve in the chosen
execution workspace. Board configuration is routing metadata, not signed task
state, authority, process isolation or an access-control boundary. Archive does
not cancel tasks or stop running Workers. No Store schema migration is added.
See [operations](../docs/reference/task-board.md#named-local-workspaces) and
[ADR-046](../docs/adr/ADR-046-named-board-workspace-routing.md).

## Required verification

Verify creation/adoption, persisted and temporary selection, explicit workspace
precedence, cwd restoration, archive/restore, exact pin rejection, key privacy,
partial setup retry and concurrent registry writers. Exercise signed same-name
tasks and Assets in separate installed domains, reopen and Redis rebuild. Run
affected tests, architecture constraints, full native-Redis regression, lint,
format, build, source-external artifact checks and documentation governance.

## Closure

Focused and full tests, independent review and source-external installed
metadata workflows passed; see
[verification scope](../reports/100-named-local-board-verification-2026-10-09.md).
This workspace convenience does not establish current
real-provider task acceptance, whole Kanban parity or stable-release readiness.
