# ADR-020: Tool closed-loop productivity primitives

Status: Accepted
Date: 2026-08-23
Related: ADR-002, ADR-006, ADR-015, ADR-019

## Context

The runtime's productivity loop is a sequence of ordinary authenticated
Contracts: a Worker requests a tool, a ToolWorker commits a local observation,
the runtime publishes a continuation, and a distinct Worker may verify the
result. Without an executable tool contract, a descriptor can drift from the
handler, malformed inputs can reach local code, and oversized or malformed
results can be mistaken for useful evidence. Without a durable projection,
operators must reconstruct tool attempts and continuations from process logs.

AI4S acceptance needs to demonstrate this loop using runtime primitives rather
than a special example DAG or a direct Store completion path.

## Decision

`ToolSpec` is the local executable contract. Its input schema, output schema,
version, and maximum UTF-8 result size are validated at registration and
execution. A signed tool descriptor must match those fields before the handler
runs. Tool execution returns a canonical observation Candidate with structured
metadata; failures remain observable and are not converted into business
outputs.

`project_task_productivity` is a read-only projection over the immutable
Contract lineage and durable RuntimeRecords/trace. The CLI exposes it from
`task audit --json`; it is an operator view, not a scheduler, budget owner, or
second commitment path.

The runtime-only AI4S acceptance path uses the public Fleet launcher and the
canonical WorkerHost compiler. A root `/plan` creates staged planning tasks;
the staged compile blueprint creates a tool-bearing producer and a verifier;
the producer's tool observation enables a continuation; the verifier submits
`/attest` for the exact report Asset; and the root is qualified only after the
committed facts survive SQLite reopen.

## Consequences

- tool input and output failures are typed, bounded, and auditable before
  they enter the continuation loop;
- descriptor drift fails closed before invoking local handler code;
- productivity views can be rebuilt from authoritative facts and do not depend
  on a live Fleet process;
- the AI4S loop is testable without treating an example-specific audit driver
  as runtime truth;
- tool observations remain local evidence and cannot satisfy a declared
  business output by name or schema alone.

## Non-goals

This decision does not add a production MCP transport, process-level timeout,
cancellation, or isolation; exactly-once effects for external systems; or
cross-machine Store/Worker discovery. Those require separate transport,
deployment, and distributed-systems decisions.

## Evidence

- `tests/test_tools.py`
- `tests/test_tool_worker.py`
- `tests/test_task_productivity.py`
- `tests/test_ai4s_runtime_primitives.py`
- `reports/056-tool-closed-loop-productivity-2026-08-23.md`
