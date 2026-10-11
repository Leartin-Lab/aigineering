# Review-linked revisions and exact input binding

Date: 2026-10-04
Status: Development slice verified; stable-release qualification remains open.
Version: 0.5.13.dev0

## Behavior and evidence scope

[Change 044](../changes/044-exact-input-binding-reader-gates.md) adds signed exact
input-role bindings, task:v6 and a schema18 reader gate. [Change 045](../changes/045-review-linked-task-revisions.md)
adds an exact changes-decision revision through canonical cancellation, allowance
reservation, successor publication and fresh acceptance. SQLite remains authority;
Redis remains required operational collaboration infrastructure.

The implementation reuses pure input selection for disclosure/verification and
successor construction for handoff/revision. Immutable reference preflight and
projection share one validator. No claim reset, mutable task status, new root grant,
new frozen policy evidence or plugin-owned qualification is introduced.

## Current verification

The author focused collection passed 37 tests, including exact input selection,
original signer/trust/redaction/slice checks, ordinary tool continuation, handoff
regression, revision replay/retained qualification and writer competition. Separate
CLI/upgrade focused tests passed 14 cases; their Redis boundary is stubbed. These
are author checks, not independent review or provider execution.

The integrated source collection passed **1,824 tests**, with no skips and six
warnings. Ruff passed and 387 Python files passed the format check. The 419-file
source, test, script and conformance map has SHA-256
`e22eecfe1cb00224c7c63b68d0773b4d49589d2ab948099f99cafda256d64539`.

Independent read-only review found no implementation blocker. Its intermediate
collection passed 142 tests and failed one governance check while this report was
absent; final governance subsequently passed all 48 cases. Independent review
confirmed every file in the integrated source map and reran the old-wheel probe
in a fresh directory. The final review manifest SHA-256 is
`f3a7bfca8c1ce7e201e7865dc51521a6d0269641eeec857fa8f150f1047322ba`.
The build and installed checks below use the same unchanged runtime sources.

A source CLI run using real isolated Redis and explicit fixture Workers passed
changes → revision → new output → new review → accept → export. It checked original
required-signer inheritance, later same-name input exclusion, source cancellation,
fresh successor qualification, keyed replay, reopen, rebuild and both event scopes.
An earlier run failed legitimate-attestation preflight because reference arrays
were compared inconsistently as lists and tuples. The corrected comparison passed;
the failed log, Store and raw decision were retained.

Three signed public revision vectors cover nullable/explicit budget and exact
replay after cancellation. Python projection matches expected facts; Node checks
canonical signing bytes, Ed25519 signatures, Candidate/fact IDs and full v6 child
identity. Two additional binding identity vectors contain no signed effects;
signature checks are explicitly not applicable.

A real previous wheel (SHA-256
`514e773e28372cd497f2b4f6c90eddb8837e19fe8c89a0d1a94ee9085f5f0db4`)
verified a signed v6 Candidate but rejected canonical declaration. It refused to
reopen schema18. Migration of a real schema17 installation database preserved
complete historical table row sets, and injected version-write failure rolled back.
Historical v3/v4/v5 IDs and canonical bytes were unchanged. Final independent probes matched the source map and passed these same checks.
A probe-harness attempt missing the extracted old site was retained before the
corrected isolated rerun; it was not a runtime migration failure.

## Retained real continuation

The source from [report 077](077-policy-bound-task-review-2026-10-04.md) and its
negative producer/reviewer reports were retained. A signed revision referenced
exact changes decision
`record:40ae9b02c546a170e11a6ddb4731c33bd9d43a4564723955ff5ca041748fa8d0`,
cancelled that source and reserved its existing seven allowance units for
`task:v6:fe6ee070adfa56aff7b1b5fd49f1ffcc9b7de00a110ec2ec797f9c666a4cc0e6`.
No fresh root grant was issued. Input roles bind the original public documents
and rubric, with only the old exact target and report added as context.

DeepSeek Flash committed a new ordinary output using 14,297 prompt and 2,598
completion tokens (16,895 total). Exit status reflected pending independent
qualification, with no invocation/encoding/commit failure. Root-agent operator inspection found
invalid CLI recipe options and premature export before qualification. This is
retained negative content evidence, not accepted productivity. The frozen public
source snapshot also preceded an earlier command-example correction; it cannot be
silently replaced. A new ordinary reviewer request reserved one successor allowance unit. The
reviewer consumed 14,366 tokens and supported the confirmed changes. A second
signed revision transferred the remaining six units without a new root grant.

The second producer used 22,999 tokens and its reviewer used 14,002. Although the
reviewer recommended acceptance, root-agent CLI execution failed on `aig init`; the
correct entry is `aig domain init`. The incorrect spelling came from the operator's
previous correction instruction, and the reviewer missed it. The root agent, acting under the user’s goal authorization, rejected that exact
target by a signed changes decision. This is retained operator error and
negative review evidence, not an accepted report.

A third signed revision transferred five remaining units. Its producer used
28,037 tokens. Actual domain initialization, Asset creation and task creation
with the report's policy JSON passed in a separate workspace without a provider
call. A new ordinary reviewer reserved one existing unit and used 14,314 tokens. The
root agent independently inspected the report and the review, then published the
policy-bound accept decision under the user's goal authorization. This is an
agent-operated decision, not evidence of a separate natural-person review.

The third successor completed with fresh qualification for exact target
`asset:v1:9a0fb2d6cbff7865cb3ae90f97d3f341651cb2692922c2a63b36db334d0b8da8`
and decision
`record:26607af64909c8d4dcce7837be73df763fb8ae9ff9abba8cf06fb1ad81accebd`.
The exported Markdown matched committed content, SHA-256
`ac7ffc53a314229ff43b9dec856dd33ea90f43f375767c5415e237ef01867a30`.
All three predecessor attempts stayed cancelled. Separate CLI process reopens,
Redis rebuild and exact decision replay preserved the final completed result.
No new root grant was added; terminal completion extinguished residual allowance.

This slice made six real DeepSeek Flash calls totaling 110,613 reported tokens,
including both rejected attempts and all reviewer runs. Monetary cost is unknown;
allowance units are not token billing. These counts exclude the earlier calls in
report 077. The accepted report summarizes its frozen historical source snapshot;
its then-future revision text does not describe the final implementation here.
Acceptance is a scoped authorized assertion, not universal semantic truth.

The direct invocation used public-only disclosed inputs and no tools. Its 190-file
runtime map SHA-256 is
`e9946d9d2d4eff6252e12fa6f2115f9db0f1a190e6a70504649f5d929d48ca48`.
The producer prompt-preview SHA-256 is
`53c363a12350164c31d15371ab8f91456504261cef77090f65523e2354ca1b03`.
Credentials, identities and raw local Stores are absent from public artifacts.

## Artifact verification and remaining qualification

The wheel was installed into a new environment outside the source tree. Separate
installed CLI processes passed the review-linked revision flow, exact signer/input
inheritance and same-name exclusion, cancellation preservation, fresh qualification,
result export, replay, reopen, Redis rebuild and both task event scopes. Existing
configured-run, task/comment/hold/handoff/review, artifact and method checks also
passed. These installed checks used explicit fixture Workers and zero provider
calls; they are separate from the six real calls above.

The verified wheel SHA-256 is
`6e4902c3825c54fb6ae459d8b1a08c8aaac25d5392727185df93801eae66d165`.
Its 190 runtime Python files matched source. The source distribution contained
fresh public documents, vectors and installed scripts; privacy checks found no
private paths, private-document references, credentials or raw local Stores.
The installed diagnostic rebuild preserved runtime records; only derived Worker
claim/registration tables differed as expected. Root-guide direct reference
validation passed 36 references. The documentation-only final rebuild retained
the exact installed wheel bytes; all 140 packaged Markdown files matched current
source. Wheel and source-distribution metadata checks passed.

Schema18 prevents normal old-reader reopen, including databases with no v6 work;
stop old Workers and use `domain upgrade` to back up before other writable commands.
The gate does not protect already-open old connections or arbitrary raw imports.

Expanded-tree handoff and the remaining assignment, priority, dependency, bulk,
board, dispatch, notification and retention work remain in the Kanban goal.
Historical missing descendant terminals still need authorized repair. The recovery
precheck/publication cancellation race and a dedicated cancellation-versus-Worker
submission barrier test remain stable-release gates. Revision-versus-cancel and
revision-versus-revision tests do not establish that separate race.

All changes are local. No commit, remote branch update, tag or package publication
has occurred for this slice. No claim of complete Kanban, stable release, universal
semantic correctness or independent natural persons is made.
