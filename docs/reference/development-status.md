# Support and limits

The current development branch is **dev**, scoped to bounded local single-machine
CLI workflows. Snapshots are identified by commit; each delivery
claim depends on evidence for the applicable case and scope. This is an
unreleased development snapshot; existing Python integrations should consult
the [import migration notes](../../changes/133-cli-guidance-and-runtime-contraction.md#python-import-migration).

| Area | Current scope |
| --- | --- |
| Local runtime | Signed Candidates, transactional SQLite facts/claims, rebuildable views; Redis is required application infrastructure. |
| CLI and task board | Explicit file inputs, configured runs, task discovery, status, claim inspection, holds, assignment, review, successors and exact export. |
| Dynamic work | Staged `/plan`, recursive replanning, tools, continuation and bounded recovery through ordinary tasks. |
| Skills | A `skill.toml` manifest plus selected content Assets; context does not grant capability or authority. |
| Existing Harnesses | Signed claim/renew/submit adapter; internal model/tool orchestration may remain with the Harness. |
| MCP | Capability metadata and callable adapters; `mcp add` does not connect a server. General production transport is not supplied. |
| Evidence | Exact Asset versions and recorded lineage; business truth and complete semantic dependencies are not inferred automatically. |
| Acceptance | Frozen task policies can require distinct authorized verifiers; different identities can still share model and source biases. |
| Workspace delegation | Explicit Engine Fleet profiles can delegate to another initialized local domain under the same trusted operator; receiver Fleets cannot contain Engine adapters. |

## Operational boundaries

- Use a fresh workspace for an independent configured run. Resume freezes the
  committed task and inputs; it does not replace them with current local files.
- New application Contracts default to contract-tree output provenance.
  Explicit policies and historical Contracts retain their stated semantics;
  a mechanical policy without `output_source` uses legacy same-name behavior.
  Schema22 prevents old readers from reopening these workspaces; stop older
  Workers and back up before upgrading.
- Independent qualification can bind an exact claim-bound output from another
  task tree. Same-name assets alone do not establish qualification.
- Tool scope is permission: repeated calls remain separate funded tasks, and
  scope alone does not require a call. Recovery names do not set a retry limit;
  causal funding still applies. Method recovery retains exact source-history
  checks and a bounded proof depth.
- Submission and invocation finish do not establish business completion.
  Scheduled recovery must be claimed and completed as new work.
- Provider request timeout, run wait timeout, retries, causal allowance and
  token usage are separate limits. Unknown usage is not zero or a known bill.
- Required Redis outages are visible. Accepted SQLite facts are preserved;
  restore relay synchronization before replaying the exact intent.
- Root retry funding has explicit limits. A retry cannot mint a fresh grant;
  general transfer/freezing semantics remain unresolved.
- An earlier intermittent Fleet stop remains unexplained. A later passing run
  does not resolve that observation or establish general long-run stability.
- Three historical calibration trials yielded zero qualified deliveries. Keep
  that result visible; later source changes do not retroactively qualify them.
- Tools run locally without a production process-isolation guarantee. The
  reference HTTP service requires deployment-level security controls.

General task reliability, unattended long runs, history-independent hot paths,
cross-machine deployment, exactly-once external effects and measured quality or
cost advantages are not established. A bounded successful case does not qualify
those broader claims. Historical failed cases are not erased by later fixes.
Case reports remain tied to their tested artifacts; the [current bounded 0.5.14
CLI case](../../reports/cli-research.md) describes its scope and limits.

Start with the [CLI research recipe](../../examples/cli_research/README.md),
and use [task operations](task-board.md) for blockers and recovery. The
[calibration case](../../reports/calibration-review.md) illustrates a retained
unqualified outcome and the limits of model protocol compliance.
