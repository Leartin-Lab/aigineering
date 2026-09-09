# v0.5.11 declarative method baseline evidence

Date: 2026-09-09
Environment: macOS arm64, Python 3.11.15.
Scope: deterministic local development verification and installed-wheel smoke.

## Implemented closure

Versioned, bounded method packages are ordinary Assets. Exact dependencies and
input bindings compile into ordinary Contracts with explicit tool grants. The
seed authoring method creates candidate packages and revisions through the same
Worker protocol. No hidden LLM loop, code installer, new kernel effect or mutable
method registry is introduced.

Separate test suites compile into ordinary case Contracts. A deterministic signed
Worker assesses their outputs, and a separate verifier attests the exact report.
Default reuse reconstructs the evidence and requires qualification for the exact
method Asset. Failed, copied or forged reports cannot enable reuse.

The lifecycle regression includes a failed version, feedback-driven revision,
successful tests, independent attestation, exact-version reuse and SQLite reopen
and rebuild. Negative tests cover hidden context, dependency authority widening,
forged run bindings, evaluator self-attestation, cancelled runs and ambiguous JSON.
Architecture tests keep methods outside the runtime kernel and prohibit direct
durable publication paths in the adapter.

## Verification

- Ruff lint and formatting passed for source, tests, scripts and the example.
- A serialized full suite passed 1,339 tests with 3 optional skips in 55.76 seconds,
  including the ambiguous-JSON and held-out-context regressions and all 9 method
  lifecycle tests.
- Isolated wheel and sdist build passed; the final local artifacts also passed
  metadata validation with Twine.
- Fresh installed-wheel smoke passed, including the method lifecycle launched
  outside the source checkout, independent CLI processes, and reconstruction.
  Its published evidence contains identifiers and outcomes; temporary domain
  keys and the fixture database are removed with the smoke workspace.

## Limits

The example explicitly requires `--worker fixture`. These checks establish the
engineering lifecycle, not live-model quality, general method correctness or a
productivity improvement. Separate local signer keys demonstrate actor separation,
not independent organizations or independent reasoning. Suite design and method
generalization remain subjects for subsequent cases and v0.6.0 experiments.

The package format, commands and supported limitations are described in
`docs/methods.md`, ADR-023 and change 021. Remote CI is a separate promotion gate;
local results do not substitute for its outcome.
