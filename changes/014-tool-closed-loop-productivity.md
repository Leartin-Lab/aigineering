# Change 014: Tool closed-loop productivity primitives

Status: Implemented and verified
Target: v0.5.6
Decisions: ADR-020

## Problem

The local tool loop already committed observations and continuations, but its
tool contract did not carry enough executable schema and resource metadata for
safe, inspectable productivity reporting. AI4S acceptance also needed a
runtime-only proof that tool observation, continuation, independent review, and
Store reopen were one canonical loop.

## Implemented change

- `ToolSpec` now carries input/output schema, version, and a UTF-8 output byte
  limit. The supported deterministic JSON-schema subset is validated before a
  handler runs and before a JSON result becomes a successful observation.
- Signed tool descriptors are checked against the registered executable
  contract before invocation. Tool observations carry durable execution
  metadata including tool version, duration, result bytes, error type, and
  retryability.
- `aig task audit --json` exposes a read-only lineage productivity projection
  derived from Contracts, Assets, RuntimeRecords, and trace. It reports tool
  calls, continuations, recoveries, rejections, terminal statuses, and usage;
  it does not create scheduler state or mutate the Store.
- The runtime-only AI4S/Fleet acceptance path exercises staged `/plan`
  compilation, a tool observation, a continuation, an independent verifier
  `/attest`, root completion, and SQLite reopen through the public Fleet/Worker
  path.

## Deliberate limits

This change remains a single-machine runtime primitive. It does not provide a
production MCP transport, process-level timeout/cancellation/isolation,
exactly-once delivery for external side effects, or cross-machine Store and
Worker discovery. Tool observations remain local evidence and never become a
business output merely because their shape is valid.

## Verification

- `tests/test_tools.py`
- `tests/test_tool_worker.py`
- `tests/test_capability_descriptors.py`
- `tests/test_task_productivity.py`
- `tests/test_ai4s_runtime_primitives.py`
- `reports/056-tool-closed-loop-productivity-2026-08-23.md`
