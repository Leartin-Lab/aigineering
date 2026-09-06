# Change 016: Reproducible release evidence

Status: Implemented and locally verified
Target: v0.5.8
Decision: ADR-021

## Scope

- Backup-first reconstruction evidence in an application adapter, with a
  read-only source, private copies, explicit mismatch/error outcomes, and no
  raw contents in the manifest.
- Python 3.11–3.13 API/Redis CI, installed-wheel smoke checks, and publication
  of the same validated distribution artifact.
- A signed-publication local scaling benchmark and AST kernel dependency
  guards that supplement existing behavior tests.

## Closure criteria

Passing local deterministic tests, build and installed-wheel checks; recorded
benchmark commands/results and explicit limitations; evidence retention for
injected reconstruction mismatch and exceptions; unchanged source databases.
Remote CI configuration is distinct from an observed remote passing run.

## Deferred work

This change does not optimize projection-context loading, isolate tools,
implement MCP transport, or coordinate external side effects. The historical
v0.5.6 mismatch remains unexplained until a root cause is established.
