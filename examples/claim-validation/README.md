# Deterministic claim validation methods

This example packages two narrow `method-package-v1` checks as ordinary
methods. `original-text` verifies exact claim IDs and byte-for-byte
`original_text` preservation. `evidence-bindings` verifies explicit excerpt IDs
and that every binding quote is a literal substring of its source excerpt. Both
methods return `semantic_checked: false`: they prove declared identity and
literal evidence binding, not whether a rationale is persuasive or whether a
verdict is substantively correct.

The cases are independent hand-written fixtures. Together the suites include passing
cases and failures for altered or missing claims, duplicate IDs, unknown
references, missing bindings, and fabricated quotes. The expected outputs are
part of the held-out suite cases and are not sent to Workers during execution.

Import and run each method through the normal CLI method lifecycle:

```bash
aig domain init
aig method import /path/to/aigineering/examples/claim-validation/original-text.method.json --json
aig method import /path/to/aigineering/examples/claim-validation/evidence-bindings.method.json --json
aig method cases /path/to/aigineering/examples/claim-validation/original-text.cases.json \
  --name original-text-cases --json
aig method cases /path/to/aigineering/examples/claim-validation/evidence-bindings.cases.json \
  --name evidence-bindings-cases --json
aig method test ORIGINAL_METHOD_ASSET_ID --cases ORIGINAL_SUITE_ASSET_ID \
  --name original-text-evaluation --budget 1 --json
aig method test EVIDENCE_METHOD_ASSET_ID --cases EVIDENCE_SUITE_ASSET_ID \
  --name evidence-bindings-evaluation --budget 1 --json
aig fleet run --config /path/to/aigineering/examples/claim-validation/workers.toml \
  --task ORIGINAL_TEST_ROOT_ID --wait-timeout 300 --json
aig fleet run --config /path/to/aigineering/examples/claim-validation/workers.toml \
  --task EVIDENCE_TEST_ROOT_ID --wait-timeout 300 --json
aig method assess ORIGINAL_RUN_ASSET_ID --json
aig method assess EVIDENCE_RUN_ASSET_ID --json
```

The method inputs are the `source` and `report` Assets. For a real invocation,
the report claims use `claim_id`, `original_text`, and
`evidence_excerpt_ids`; evidence validation additionally requires
`evidence_bindings` entries of `{excerpt_id, quote}`.

An assessment with `passed: false` is a successful execution of a validator
that found a defect in its report input. It does not automatically fail or
stop a larger root business task. A root workflow that requires fail-closed
behavior uses the separately configured structural gate below. It rechecks the actual
disclosed source and report; it does not trust a model-authored assessment. The
two method packages contain no tools, dependencies, or extra authority.

The local Worker factories are explicit and use the `validation` pool. No API
key or external service is required.

## Blocking structural gate

The included gate factory reads the ordinary declared inputs `research_result`
and `review_report`. It reruns both checks, returns `/fail` on a structural
violation, and publishes an exact-input receipt only on success. Its receipt
explicitly says `semantic_checked: false`; it is not an independent semantic
attestation and cannot replace the original example's semantic verifier.

```bash
aig asset add --name research_result \
  --content-file /path/to/aigineering/examples/claim-validation/gate-source.json --json
aig asset add --name review_report \
  --content-file /path/to/aigineering/examples/claim-validation/gate-report.json --json
aig task create --name structural-gate \
  --description 'Check exact original claim text and explicit evidence bindings.' \
  --input research_result --input review_report \
  --activation 'research_result AND review_report' \
  --output structural_receipt --budget 1 \
  --requires-capability claim.check.gate --worker-pool validation --json
aig fleet run --config /path/to/aigineering/examples/claim-validation/workers.toml \
  --task GATE_TASK_ID --wait-timeout 30 --json
aig task audit GATE_TASK_ID --json
```

Use a fresh domain when changing the gate inputs; multiple same-name Assets are
ambiguous and fail closed. A planner can declare the same input/output contract
with `claim.check.gate` and `validation` inside its delegated scope. Business
work requiring the gate must depend on its receipt; merely running an assessment
in parallel does not enforce a dependency.

## Independently reviewed method reuse

After inspecting a passing method evaluation, use the existing reviewer command:

```bash
aig method attest EVALUATION_ASSET_ID --verdict accepted --json
aig method instantiate METHOD_ASSET_ID \
  --input source=SOURCE_ASSET_ID --input report=REPORT_ASSET_ID \
  --output assessment=reused_assessment --name reuse-check --budget 1 \
  --evaluation EVALUATION_ASSET_ID --json
aig fleet run --config /path/to/aigineering/examples/claim-validation/workers.toml \
  --task REUSE_TASK_ID --wait-timeout 30 --json
```

Review authorizes the exact tested method, not arbitrary reports. Different keys
under one local operator do not establish organizational independence. Installed
factory code is trusted operator code, loaded only from fleet configuration;
method import never loads code. See [ADR-024](../../docs/adr/ADR-024-local-validation-workers.md).

Recorded CLI execution, qualification, reuse and gate outcomes are in
[report 064](../../reports/064-claim-validation-methods-2026-09-13.md).
