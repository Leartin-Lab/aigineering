# Roadmap

The current source is **v0.5.14**, scoped to bounded local single-machine CLI
workflows. Version metadata does not establish delivery qualification; claims
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

Planning, tool continuation, retry and recovery remain ordinary runtime tasks.
These priorities do not introduce a second scheduler or expand kernel authority.

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
