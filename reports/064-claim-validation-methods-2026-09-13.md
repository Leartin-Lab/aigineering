# Deterministic claim-validation methods — 2026-09-13

The two validation methods in [this example](../examples/claim-validation/README.md)
ran through the existing method lifecycle with local deterministic Workers.
Four original-text cases and five evidence-binding cases matched their separately
written expected results. Both evaluations received independent local reviewer
attestations and authorized a subsequent exact-method reuse invocation.

The blocking structural gate passed a valid synthetic report and terminated as
`failed` for each of the four unchanged historical claim reports from
[report 063](063-runtime-native-cases-2026-09-13.md). The successful gate receipt
binds the actual source/report Asset IDs and explicitly says
`semantic_checked: false`. Failed gates produced no success receipt.

## What ran

Implementation: v0.5.11 development tree based on `479ecee`, with
[change 023](../changes/023-local-validation-workers.md) and
[ADR-024](../docs/adr/ADR-024-local-validation-workers.md). This work adds an
explicit local Worker factory option to the Fleet launcher and business-layer
checkers. No runtime kernel effect or Store schema changed.

The operator imported the checked-in method packages and case suites with the
CLI, then used `method test`, `fleet run`, `method assess`, `method attest`, and
`method instantiate --evaluation`. Test child Contracts were created by the
existing method-test compiler, not by a business driver. Assessment used the
existing deterministic evaluator, and review used the separate local method
reviewer key. Each reuse ran through the normal fleet after qualification.

The five gate runs each started with a fresh domain, two input Assets, one
ordinary root Contract, and one `fleet run`. These are targeted validation runs;
they do not repeat the earlier LLM `/plan` workflow. No provider was called, no
API key was needed, and no model output was edited to make a gate succeed.
The positive gate input is explicitly a synthetic fixture.

The gate follows the existing failure protocol: `/fail` creates one ordinary
failure-report child, which the Worker completes with `/exec`. The existing
completion plugin then closes the parent as `failed`. Negative verified runs
contain two Contracts, not an unbounded failure loop. An initial adapter trial
repeated `/fail` while processing failure-report work and timed out; handling
that work was fixed, covered by a terminal-state regression test, and all gates
were rerun in fresh domains. The final evidence below is from those reruns.

## Results and evidence

| Exercise | Result | Evidence |
| --- | --- | --- |
| Original-text method | 4/4 expected results; qualified and reused | [Method evidence](data/064-claim-validation/methods.json) |
| Evidence-binding method | 5/5 expected results; qualified and reused | [Method evidence](data/064-claim-validation/methods.json) |
| Valid synthetic gate | Complete, exact-input structural receipt | [Gate evidence](data/064-claim-validation/gates.json) |
| Initial historical claims | Failed, no receipt | [Gate evidence](data/064-claim-validation/gates.json) |
| Source-preserving historical claims | Failed, no receipt | [Gate evidence](data/064-claim-validation/gates.json) |
| Citation-tightened historical claims | Failed, no receipt | [Gate evidence](data/064-claim-validation/gates.json) |
| Taxonomy-defined historical claims | Failed, no receipt | [Gate evidence](data/064-claim-validation/gates.json) |

A validator returning `passed: false` is a successful assessment of a defective
report. Its method test passes when that assessment equals the held-out expected
result. This is different from the gate, which uses terminal failure to prevent
a successful receipt. Work requiring the gate must explicitly depend on that
receipt; a parallel assessment alone does not gate anything.

## Exact scope of the checks

Original-text validation detects changed text, duplicate IDs, missing claims and
malformed identity fields. It rejects the initial historical report's three
rewritten `original_text` fields. It does not reject the other historical reports
on original-text grounds, because their exact originals were preserved.

Evidence-binding validation requires each claim to list existing unique excerpt
IDs and explicit `{excerpt_id, quote}` bindings. Quotes must be literal substrings
of the corresponding source excerpt. Binding and citation ID sets must match;
unknown IDs, duplicate identical bindings, fabricated quotations and missing
bindings fail. All four historical reports lack this new binding structure, so
all fail that declaration check. This must not be described as automatically
finding the implicit semantic citation omission in their free-text rationales.

A report can preserve originals and quote real passages while still drawing an
incorrect conclusion. These methods do not determine causal validity, semantic
support, completeness of the reasoning, correct verdict choice, or whether a
mean comparison supports a probability claim. There is no semantic attestation
from these Workers. Stronger semantic validation remains separate work.

## Reconstruction and trust

Backup-first diagnostics were run against the method lifecycle store and the
five final gate stores. The exported evidence records matching before/after
materialization digests and unchanged durable RuntimeRecords. Rebuild may
normalize projection table bytes; this is semantic reconstruction evidence.

Worker code is explicitly installed and configured by the operator. Method
packages cannot load it or expand its authority. Test cases are separate from
method instructions and expected answers are not disclosed to case Workers.
A separate local review key demonstrates authority separation under one
operator, not independent organizations. These small regression suites establish
bounded behavior rather than general reliability.

## Repository validation

The full local test run passed with 1,368 tests and 3 optional skips. Ruff lint
and format checks passed. The isolated source/wheel build succeeded. Integration
coverage includes native method evaluation, independent qualification, evaluated
reuse, materialization rebuild, and durable gate failure after exactly one
failure-report child.
