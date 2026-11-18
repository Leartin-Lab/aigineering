# Roadmap

The **dev** branch supports bounded local single-machine CLI workflows.
Development snapshots are identified by commit. Delivery claims
depend on evidence for the applicable case and release scope. See
[current support](docs/reference/development-status.md).
The implemented boundary is actor-signed Candidate commitment in a single-machine
runtime, with transactional SQLite facts and required Redis collaboration.

## Priorities

1. Make supported end-to-end scenarios easy to configure, run, inspect and export.
2. Improve CLI/board guidance, model action discipline, exact Asset evidence,
   timeout configuration and actionable failure diagnosis.
3. Make existing Harness, Skill and tool-adapter integration explicit and testable.
4. Reduce duplication and unnecessary maintenance; address confirmed performance
   defects after the supported user path works.

Planning, tool continuation, retry and funded recovery use ordinary runtime
tasks. The runtime does not maintain an execution DAG or post-completion
controller. Unfunded recovery remains a visible blocker; no root grant is
fabricated.

Current control-plane scope is intentionally limited to ordinary task creation,
execution, claims, Assets, attestation, cancellation, retry and diagnostics.
Legacy handoff, revision/edit, assignment, dependency, hold/promotion, priority,
comment and attachment-link records are historical read surfaces, not fresh
writer workflows.

## Outside current scope

General autonomous delivery, production MCP transport, process-level tool
isolation, exactly-once external effects, multiple database backends and a
distributed platform are not promised by the current local implementation.

## Release gates

Stable acceptance requires useful real tasks on a fixed installed artifact,
appropriate failure/restart/rebuild/concurrency checks, qualified outputs and
clear limits. Deterministic tests alone do not establish semantic quality,
lower cost or long-run reliability. See [case reports](reports/README.md) and
[contribution checks](CONTRIBUTING.md).
