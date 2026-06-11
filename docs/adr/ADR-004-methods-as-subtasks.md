# ADR-004: Methods as Subtasks

**Status**: Accepted  
**Date**: 2026-06-02

## Context

When an LLM worker decides it needs to plan, replan, or fail, traditional agent loops allow the model to trigger hidden state mutations. The model says "/plan" and the runtime invisibly restructures the task. This hides critical decisions from the audit trail.

## Decision

Aigineering treats method requests (`/plan`, `/replan`, `/fail`) as **explicit method subtasks** rather than hidden controller operations. When a worker issues `/plan`, the runtime creates a `.plan` contract, which produces a `_plan_result_` asset. The engine then projects child contracts through authority. Every method decision is auditable.

## Consequences

- No hidden state mutations — all method decisions are explicit contracts with traceable results
- Worker cannot trigger invisible restructuring of the task graph
- Method requests become first-class auditable events
- In v0.3, `/plan`, `/replan`, and `/tool` are represented as system method
  contracts. Future method handlers should keep the same auditable ingress while
  moving feature-specific behavior out of the engine core.
