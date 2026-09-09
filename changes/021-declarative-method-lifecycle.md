# Declarative methods and method creation

Status: implemented in the v0.5.11 development line.

## Changes

- Bounded, immutable `method-package-v1` and `method-cases-v1` declarations.
- Exact dependency and input binding, explicit tool grants, ordinary Contract
  instantiation and existing Worker/Fleet execution.
- A seed authoring method that creates new packages or revisions as ordinary
  Worker outputs, with explicit request and feedback context.
- Independent test suites compiled to ordinary work without exposing expected
  values to case Workers.
- Deterministic evaluation published by a signed Worker, independent acceptance,
  and consumer validation before exact-version reuse.
- CLI import, cases, show, inspect, instantiate, create, test, assess and attest.
- Rebuild/reopen coverage and a standalone explicit-fixture governance example.

No new kernel effect, database schema, privileged method executor or mutable
eligibility state is introduced. See ADR-023 and `docs/methods.md` for the local
scope, protocol reuse and test-evidence limitations.
