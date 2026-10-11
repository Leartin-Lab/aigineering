# Required-Redis CLI: real repository-report follow-up

Status: Bounded real-work execution verified; content-review defects retained
Date: 2026-10-04
Source version: 0.5.13.dev0; no release tag or publication

## Scope and evidence binding

This follows [report 072](072-task-cli-required-redis-2026-10-03.md) after current-goal
authorization for provider execution. The Python/configuration snapshot remains
`e8b337c71f8ac8285f6e692180cdc6388b6c59e23f4d1c09d4ead7c4047121a6`:
347 source, test, script, preset and build-configuration files. No runtime code
changed after its 1662-test serial suite. Before execution, independent document
review found stale optional-Redis/fallback statements in current README sections;
these were corrected and the four actual input files were frozen again.
Historical v0.5.12 optional-Redis evidence retains its historical scope.

The real task disclosed current README, DESIGN, ROADMAP and the task-operation
reference as explicit input Assets. It used the normal `aig run --config`
application, one LLM Worker and actual default Redis 7.4.11; no scripted response,
mock Worker, implicit repository discovery or example-owned scheduler was used.
The workload requested a Chinese Markdown analysis of implemented capabilities,
operational commands, limits and three practical next development steps.

The Worker profile used `deepseek-flash`, `thinking_mode = "disabled"`,
`response_format = "json_object"`, 8192 maximum output tokens, zero HTTP retries,
capacity one and a root causal allowance of one. Secrets were supplied only in
process memory/environment. Causal allowance is not provider token billing.

## Result and reconstruction

- Root: `task:v3:9289961adc4767c52f81d4c5c96e9e3711a1fc8db7c28e577fd714b2154b61f6`.
- Output: `asset:v1:893fd5dd6a08757771f23e1df4469bea4d5c25167cf92641214e40a78cbe0e8a`.
- One actual provider call: 16,682 prompt tokens plus 2,373 completion tokens,
  19,055 total, as reported by the provider and retained in the task audit.
- The root completed without timeout, commitment rejection or recovery.
  Local runtime-record/materialization integrity passed.
- `task result` exported exactly 8,857 committed UTF-8 bytes. SHA-256:
  `5c73f3d2a324b64285d38069197778fe2b1dfe1c675ead50a23943a63cbb5d08`.
- A separate CLI process resumed the same completed root with its Worker profile;
  completion and usage records were unchanged, with no additional model call.
- Backup/reopen/reconstruction preserved records and matched semantic digest
  `67443cd20a762763739437ca36d1f7dcf8166df704f9ef6a743940bb3231441b`
  before and after rebuild.

The original report and audit remain available privately. The public evidence
contains stable IDs, hashes and usage, not raw Stores or private workspace paths.

## Preparation failure and independent content review

An initial operator-created preset used `&` rather than the required uppercase
`AND` activation grammar. Preparation rejected it before creating a Store or
calling the provider. Its diagnostic was retained; correcting the preset led to
the single provider execution above. This was an input-validation failure, not a
failed provider run or silent repair of a model response.

Independent read-only review by GPT-6 Luna found the main architecture and
runtime boundaries broadly accurate, but two practical defects in the generated
instructions: an ordinary-task recipe created work and then waited without an
execution command, and later queries/exports omitted the workspace selected for
the earlier preset. The current preset output is `audit_report`; a generic
example's `report` output name is not interchangeable with it. The report's
pending-provider statements describe its pre-call input snapshot, not the state
after this execution.

The committed model output was not edited or regenerated to conceal these
defects. A separate operator-reviewed correction note explains consistent
workspace selection, explicit `run --task`, output slots and Worker credential
configuration. It was published as a normal signed human Asset:
`asset:v1:8a769fd1df9f4137721d8b3c078098c537b7e80c033a3f88affd7aef6ba70705`.
After that publication, a second reconstruction again preserved records and
matched digest
`2223647e80482f1ddadfa839cfa97e2010c9d6aaba0da2662ec24552ed29eb72`.
This is documented content review and an authorized correction assertion;
neither is a runtime independent-policy attestation.

## Qualification limits

This verifies one actual configured task with required Redis, signed output,
precise export, completed-root resume and reconstruction. It does not prove
general report correctness, improved model quality, full Hermes feature parity,
execution of all presets or current-snapshot long-chain/nested live gates.
The task had no independent acceptance policy; successful commitment and
completion are authorized assertions under that Contract, not semantic truth.

Current deterministic/real-infrastructure/installed evidence and this bounded
provider result are reported separately. Stable qualification, release tagging
and package publication remain separate work. Delivery remains local edits,
built artifacts and local infrastructure; no new commit or remote update was made.
