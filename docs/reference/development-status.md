# Support and limits

The current development branch is **dev**, scoped to bounded local
single-machine workflows. This is an unreleased snapshot; delivery claims
depend on evidence for the exact artifact and case. Python integrations should
consult the [primitive runtime migration](../../changes/137-closed-primitive-task-runtime.md#public-compatibility)
and [earlier import changes](../../changes/133-cli-guidance-and-runtime-contraction.md#python-import-migration).

| Area | Current scope |
| --- | --- |
| Runtime | Signed Candidates, transactional SQLite facts and claims, rebuildable views; Redis is required application infrastructure. |
| Tasks and CLI | Create, run, inspect, cancel, retry, review and export ordinary tasks and Assets. |
| Output matching | Any admitted Asset may meet frozen name, Definition, content, producer and shape conditions. No root, origin or task-tree completion gate. |
| Planning and tools | Bounded ordinary child tasks; tool calls require real causal funding. Scope permits but does not require a call. |
| Skills | A manifest and selected context Assets; context does not grant capability or authority. |
| Harness | Signed claim, renewal and submission adapter; private model/tool orchestration may remain in the Harness. |
| MCP | Capability metadata and callable adapters; `mcp add` does not connect a server. Production transport is not supplied. |
| Review | Ordinary reviewer Task and request Asset; decision commits canonical attestation and a descriptive decision Asset. |
| Evidence | Exact Asset identity and frozen selectors; business truth and complete semantic dependencies are not inferred automatically. |

## Operational boundaries

- Schema 24 is the current reader floor. Stop older Workers and back up before
  upgrading. The floor cannot constrain a connection already opened by an old
  process.
- Output requirements can select exact Definition, content, producer actor
  and shape. A matching admitted Asset from another Task can satisfy an
  output. Historical `output_source` metadata remains readable but does not
  impose a producer-tree rule. Independent acceptance still requires exact
  qualification.
- Engine bridge output Definitions describe the frozen goal and exact mapped
  inputs. Existing signed bridge history is not rewritten; an old publication
  that does not match current output conditions may require new ordinary work.
- Failure is recorded at the attempt and audit level. Retry or recovery
  requires an actually available causal grant. No root grant is fabricated.
  Bounded deterministic and installed CLI cases verify these paths; broad
  real-provider reliability remains unestablished.
- Planning, tool continuation, review and recovery publish ordinary Tasks and
  Assets. There is no post-completion controller or runtime execution DAG.
- The current fresh writer surface does not include handoff, revision/edit,
  mutable assignment, dependency edges, holds/promotion, priority writes,
  comments or attachment links. Historical records may be inspected where
  supported; they do not change current claim or fulfillment eligibility.
- Provider request timeout, run wait timeout, retry count, causal allowance
  and token usage are separate. Unknown usage is not zero or a known bill.
- Redis outages are visible. Accepted SQLite facts remain durable while relay
  synchronization is pending.
- Slice creation commits the Asset and its lineage assertion together;
  demo and quick GOAL creation commit configuration, inputs and root together.
  Configured setup likewise commits its inputs, Skills and exact-bound Contract
  in one Candidate. Same-name Tasks or historical outputs do not block creation;
  ordinary requirements determine satisfaction. Identical setup is exact replay,
  while changed resources produce a distinct intent and Contract.
- Local tools do not provide a production process-isolation guarantee. The
  reference HTTP service requires deployment-level security controls.

General unattended reliability, cross-machine deployment, exactly-once
external effects, measured quality or cost improvements and broad semantic
correctness are not established. Historical failures remain part of the
evidence. Case reports stay tied to their tested artifacts; see the [CLI
research case](../../reports/cli-research.md) and
[calibration review](../../reports/calibration-review.md).

Start with the [CLI research recipe](../../examples/cli_research/README.md) and
the [task operations](task-board.md). The calibration case illustrates an
unqualified result and limits of model protocol compliance.
