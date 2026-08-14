# ADR-019: Capability-routed local Worker fleet

Status: Accepted
Date: 2026-08-14
Related: ADR-004, ADR-006, ADR-009, ADR-012, ADR-013

## Context

A single push-driven Engine cannot express local provider choice, independent
task concurrency, or restart-safe joins without acquiring private scheduler
state. Model names and prices are also unstable task semantics. The runtime
already has the stronger primitive: immutable Contracts matched to registered
Workers through claims over one authoritative Store.

## Decision

Contract v5 separates requirements for executing the current task from the
capability and pool scope it may delegate to descendants. Planning binds child
requirements to that scope and freezes label-selected Asset IDs.

A local Fleet is an application launcher. Each capacity slot uses an independent
SQLite connection and the canonical pull, claim, invoke, and submit protocol.
The Fleet owns no task status, queue, or alternate write path. Provider profiles
are declarative TOML and reference secrets only by environment-variable name.

Parallel tool requests compile into ordinary tool Contracts plus a continuation
whose activation is the conjunction of committed observation names. Structural
and projection rejection both preserve signed Worker evidence and publish a new
recovery Contract; a failed claim is never reopened.

Tool execution capabilities are exclusive: a ToolWorker cannot claim an
ordinary reasoning continuation merely because that task has no explicit
requirements. Planned and recovery tasks with unresolved tool scope receive a
tool-dispatch-only prompt. A successful observation removes the used tool from
the continuation scope. Recovery preserves the original acceptance policy and
is bounded to three successor attempts.

## Consequences

- cheap, advanced, visual, tool, or human Workers are selected by capabilities
  and operator policy rather than embedded model identity;
- capacity creates real local concurrency while SQLite retains claim fencing and
  terminal arbitration;
- every child and join can be tested independently and reconstructed after
  restart;
- a Fleet is not a distributed scheduler, and v0.5.5 makes no cross-machine
  discovery or public-network security claim.

## Evidence

- `tests/test_local_fleet.py`
- `tests/architecture/test_commitment.py`
- `tests/architecture/test_plugins.py`
- `tests/architecture/test_independent_acceptance.py`
- `tests/test_ai4s_literature_example.py`
