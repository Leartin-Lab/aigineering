# ADR-001: Asset-Driven Execution

**Status**: Accepted  
**Date**: 2025-06-02

## Context

Agent systems need to coordinate tasks where worker outputs (from LLMs, tools, scripts) create assets consumed by downstream tasks. The traditional approach is a static DAG — pre-planning the entire workflow before execution begins.

## Decision

Aigineering uses **asset-driven execution**: tasks activate when their required input assets become available, not by DAG traversal order. The engine reacts to asset creation events.

## Consequences

- No static execution graph required — tasks can dynamically depend on outputs of other tasks
- Parent completion is determined by asset presence (outputs satisfied), not child termination
- Enables content-addressed, replayable execution where the same inputs produce the same task graph
