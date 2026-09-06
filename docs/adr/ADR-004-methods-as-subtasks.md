# ADR-004: Methods as Subtasks

**Status**: Accepted; implementation superseded in part by ADR-011
**Date**: 2026-06-02

## Context

When an LLM worker decides it needs to plan, replan, or fail, traditional agent loops allow the model to trigger hidden state mutations. The model says "/plan" and the runtime invisibly restructures the task. This hides critical decisions from the audit trail.

## Decision

Aigineering treats method requests (`/plan`, `/replan`, `/fail`) as **explicit method subtasks** rather than hidden controller operations. When a worker issues `/plan`, the runtime creates a `.plan` contract, which produces a `_plan_result_` asset. The engine then projects child contracts through authority. Every method decision is auditable.

## Supersession and implementation note

The auditable-subtask principle remains valid. ADR-011 supersedes the
method-specific implementation described here: current planning, replanning,
recovery, verification, and tool behavior is Plugin-owned and proposes ordinary
signed Candidates and Contract declarations. The current runtime does not
commit a `_plan_result_` Asset or assign lifecycle semantics from method names;
see `DESIGN.md` and ADR-011.

## Consequences

- No hidden state mutations — all method decisions are explicit contracts with traceable results
- Worker cannot trigger invisible restructuring of the task graph
- Method requests become first-class auditable events
- In v0.3, `/plan`, `/replan`, and `/tool` are represented as system method
  contracts. Future method handlers should keep the same auditable ingress while
  moving feature-specific behavior out of the engine core.
