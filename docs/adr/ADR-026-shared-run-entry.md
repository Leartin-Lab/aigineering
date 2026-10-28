# ADR-026: Shared application entry over the fact runtime

Status: Accepted for the local development implementation
Date: 2026-10-03
Related: ADR-011, ADR-019, ADR-023

## Context

Quick goals, prepared task pools and declarative examples share the commitment
kernel but expose different setup recipes. Fleet CLI assembly also lived in a
command module. Operators had to learn resource publication before starting an
otherwise ordinary configured workload.

## Decision

Use `aig run` as the common execution entry. Explicit TOML presets assemble
input Assets, Skills, a root Contract and existing Worker profiles at the
application layer. Existing `fleet run` delegates to the same configured-run
service. Workspace selection and an explicit task-ID resume option provide
familiar CLI operations without making a conversation or session log the owner
of task state.

Prepare files and adapters before provisioning the runtime. Publish resources
and the root through ordinary signed Candidates. Freeze label-selected context
and declared acceptance evidence to committed Asset IDs. Resume reads the
existing task rather than republishing setup or rebinding labels. Reject a new
start whose named outputs already exist; named sharing remains available through
ordinary task publication. A fresh workspace separates independent preset runs.

The CLI vocabulary takes inspiration from [OpenCode's noninteractive run and
continuation commands](https://opencode.ai/v2/docs/cli/commands/); shared
application assembly follows the pattern illustrated by
[Pi's session SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md).
This is an interface design reference, not a compatibility claim or adoption
of their orchestration kernels.

## Alternatives

Keeping only manual publication recipes preserves low-level control but repeats
setup knowledge across every example. A separate example command family would
create another entry to learn. Importing an external conversation engine would
add a competing lifecycle and authority owner. The shared application service
keeps manual commands available while reusing the existing Fleet launcher.

## Consequences

Configured examples can use one launch command and explicit task-ID continuation.
Configuration must remain strict and explicit; it cannot install code, grant
capabilities through labels, or introduce a hand-written business DAG. Local
factories remain trusted operator-selected code. Methods keep their declaration,
evaluation and qualification steps, then execute as ordinary tasks. Quick goals
and single-worker task pools keep their existing execution paths; this decision
does not yet collapse all runtime adapters into one event loop.
