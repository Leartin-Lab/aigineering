# ADR-005: Unified Feature Ingress

**Status**: Accepted
**Date**: 2026-06-11

## Context

Agent runtimes tend to grow by adding each new capability at the nearest
available surface: an engine branch, an API endpoint, a store field, a prompt
template, a CLI command, or a UI projection. That makes early progress fast, but
over time each capability develops its own lifecycle, authority assumptions,
trace behavior, and replay semantics.

Aigineering needs a single rule for how features enter the runtime so that
tools, planning, recovery, skills, MCP, memory, and future distributed workers
do not become hidden mutation paths.

## Decision

Every runtime feature must enter through a classified, auditable ingress:

| Feature intent | Ingress |
|---|---|
| Change or steer execution | Method |
| Execute work | Worker |
| Represent facts, evidence, policies, capabilities, or observations | Asset |
| Assemble reusable context | Label |
| Cross a runtime boundary | Protocol |
| Persist/query records | Store or Trace adapter |
| Display/debug records | View or Projection |
| Authorize minting | Authority or Policy |

The default ingress for new execution behavior is **Method**. A method may plan,
replan, call a tool, verify evidence, recover from failure, read context, or ask
for human input, but it must do so through explicit contracts, assets, budget,
and trace records.

Workers execute bounded work and return candidates. They do not directly commit
facts. Assets are durable records; they do not execute. Labels assemble context;
they do not grant authority.

## Consequences

- Feature behavior remains auditable instead of becoming hidden controller
  state.
- The engine can stay focused on activation, disclosure, candidate intake,
  projection, authority, commit, trace, and minimal scheduling.
- Tools, MCP, skills, memory, and distributed workers can be added without
  weakening the candidate-fact boundary.
- New features require an explicit classification before implementation.
