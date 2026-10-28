# ADR-046: Named local board routing

Status: Accepted for the bounded local routing workflow.

## Context

`task board` displays facts within one domain. `-C/--workspace` selects an
explicit directory. Operators working across independent projects also need
persistent names, selection and an archive list without duplicating task state.
This is a convenience for multiple workspaces, not a prerequisite for one
domain to execute and accept useful work.

## Decision

Keep named boards in application configuration. `local_boards.py` owns the
project-local `.aig/boards.json` registry and the create/list/show/switch/archive
lifecycle. Each entry pins a canonical workspace path and an exact Genesis ID.
`local_domain.py` owns explicit initialization shared by board creation and
`domain init`; the core Genesis boundary and implicit first-use identity setup
retain their existing roles.

Discover the nearest registry from the invocation directory and its ancestors.
Management operates at that registry root. Saved selection routes ordinary
commands; temporary `--board NAME` leaves it intact. Explicit `-C` bypasses
selection, and combining the two explicit selectors is a usage error. Validate
the exact Genesis pin before routing or switching. Missing, replaced or archived
selected domains fail visibly, without fallback. Restore the invocation cwd on
exit; relative command inputs resolve inside the selected workspace as with `-C`.

Create separate managed workspaces below `.aig/boards/NAME`, or deliberately
adopt an initialized explicit workspace. Creation does not select it. Reject
duplicate names, paths and Genesis IDs, including archived entries. Archive
retains the selected pointer and changes registry metadata only. Restoring it
reverses that flag; neither action cancels work, deletes files or stops Workers.

Read a bounded supported schema from private regular files, reject unsafe paths
(including symlinked managed workspace components), and serialize atomic
registry replacement. A committed Genesis survives relay or registry
publication failure. Report setup pending and recover the same name and path;
synchronize/adopt that domain rather than replace its key or Genesis.
If replacement is visible but directory durability confirmation failed, explicit
`create NAME --resume` validates the complete existing entry and exact pin,
synchronizes Redis and republishes unchanged registry metadata under the lock.
Ordinary create rejects duplicate names; resume preserves selection/archive
and refuses changed entries, paths or missing registration.
A retained root key without Genesis blocks automatic initialization.

## Alternatives

Mutable card status or a second queue would duplicate authoritative task
semantics. Aliases alone would omit persistent selection and recovery. A global
registry would introduce surprising cross-project defaults. Automatic fallback
after archive could mutate another domain. Copying bootstrap would let key and
Genesis behavior diverge between entry points.

## Consequences

SQLite facts, signed Candidates, claim fences and acceptance remain authoritative.
Redis uses the canonical domain namespace and remains required operational
infrastructure. The registry grants no capability, budget, input access or
acceptance. It is local routing consistency, not OS isolation or multi-user ACLs.
External workspace adoption does not promise discovery from an unrelated cwd.
Archive does not prevent explicitly accessing a directory through `-C`.

Lifecycle, controlled partial failure, concurrent creation and domain isolation
require bounded checks. Workspace routing does not qualify real-provider output
quality or a stable release.
