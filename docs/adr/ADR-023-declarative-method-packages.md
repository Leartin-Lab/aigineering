# ADR-023: Declarative method packages and evaluated reuse

Status: Accepted for the v0.5.11 local development implementation
Related: ADR-006, ADR-015, ADR-017, ADR-019, ADR-020, ADR-022

## Problem

Skills disclose procedural text, but reusable work also needs exact input/output
bindings, dependency identity, explicit requirements, test evidence and a way to
create revised methods from observed failures. Adding a private method interpreter
would reintroduce hidden task state and a second authority boundary.

## Decision

The `aigineering.methods` application adapter defines `method-package-v1` JSON.
Packages are ordinary signed Assets. The schema binds descriptive version,
instructions, input slots, output shapes, routing/tool requirements, exact package
and context dependencies, and optional author examples. Import parses bounded,
strict JSON; it does not install code, register tools or grant capabilities.

Instantiation resolves a bounded exact-ID closure and creates an ordinary
versioned Contract. Input bindings use exact context Asset IDs rather than a
live name lookup. Requested tools must be covered by the caller's explicit grant.
Dependencies cannot widen the root package's declared requirements. Generated
plans remain subject to the existing claim and parent containment boundary.

The seed `method-author` is itself a declarative method. `method create` publishes
one ordinary task with its instructions, exact request and optional prior method
and feedback Assets. It grants no special publication or verification authority.
The output can be any ordinary Worker-produced package; consumers parse it again.
No LLM is invoked by the create command and there is no private multi-call loop.

## Test and acceptance model

A separately published `method-cases-v1` suite freezes test inputs and expected
results. `method test` publishes case inputs, an aggregate root and ordinary case
Contracts, then a reconstructable run manifest. Expected answers are not disclosed
to the Workers executing those cases. Package examples remain author-visible and
are not claimed to be independent tests.

`method assess` compares exact outputs against expected JSON values (or exact text
for an unshaped slot). It reconstructs the expected test Contracts to reject
forged run bindings. Passing requires a successful root and successful output
producers in the appropriate case lineage; unrelated same-name outputs cannot
count. Missing or failed outputs remain failed test evidence. A new test run is
required when a case is repaired as a sibling recovery outside that lineage.

The deterministic evaluator is a normal signed Worker and publishes a report
through canonical claim/package/submit. Its Contract requires one independent
`method.verify` attestation binding the run, method, suite and observed outputs.
The evaluator necessarily reads expected values; it is distinct from the Worker
under test and from the policy-bound verifier.

Default reuse requires an explicitly selected, independently qualified evaluation
for the exact method Asset. The consumer recomputes its report and checks the
exact acceptance Contract and qualification fact. Copying a report, changing a
package version, or setting `passed` cannot grant eligibility. A caller can
explicitly request an unverified trial; the decision is frozen in its Contract.
There is no mutable global enabled/trusted flag. Qualification does not grant
permission to use tools or expand a caller's authority.

## Ownership and limits

Methods add no kernel effects, schema migration, scheduler or direct Store writes.
Local CLI publication uses the operator's signed authority. The explicit attest
command uses a separate local reviewer actor, not a producer self-attestation;
this demonstrates key separation, not organizational or cognitive independence.

Evaluations establish performance on selected fixtures, not general method
correctness or semantic truth. Test selection and generalization remain reviewer
responsibilities. No market, remote package registry, dependency download,
arbitrary-code installation, automatic policy promotion or production isolation
is included. Package dependency IDs are domain-local; JSON export alone is not a
portable cross-domain authorization or signed evidence proof.
