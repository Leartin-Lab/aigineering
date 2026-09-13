# Operator-configured local validation Workers

Status: Implemented

Fleet profiles can explicitly load installed `module:factory` Workers with
`kind = "local"`. Factory loading belongs to the application launcher and uses
the existing WorkerHost, claims, signed Candidates and commitment path.

Two reusable claim-validation methods check exact original text and explicit
excerpt/quotation bindings. They return bounded structural assessments and make
no semantic-correctness claim. A separate gate produces an observable `/fail` for
invalid inputs or structural violations, and an exact-input receipt on success.

See [ADR-024](../docs/adr/ADR-024-local-validation-workers.md) and the
[claim-validation example](../examples/claim-validation/README.md). There is no
schema migration or automatic installation of code from a method package.
