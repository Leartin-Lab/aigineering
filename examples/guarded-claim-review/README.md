# Guarded claim review

This is a declarative root-task example for a single bounded revision followed
by deterministic gating and a separate semantic review. The operator submits
three base Assets plus the frozen `review_instructions` document first: `research_result`, `draft_report`, and
`review_policy`. The root task freezes all three in context and passes them to
every child. Planner-created handoffs are `draft_checks`, `review_report`,
`structural_receipt`, `semantic_review`, and `acceptance_receipt`.

The local assessor and gate perform deterministic checks. The reviser and
semantic reviewer are configured for one model call per attempt through the
local factory, using
`DEEPSEEK_API_KEY`, `AIG_REVIEW_MODEL` (default `deepseek-flash`), and
`AIG_REVIEW_BASE_URL` (default `https://api.deepseek.com/v1`). The acceptor has
the only `asset.attest` capability. Worker roles and policy actors are distinct.

Prepare the three input Assets, then create the root with ordinary CLI commands:

```bash
export DEEPSEEK_API_KEY=replace-me
export AIG_REVIEW_MODEL=deepseek-flash
export AIG_REVIEW_BASE_URL=https://api.deepseek.com/v1

aig domain init
aig asset add --name research_result --content-file research-result.json --json
aig asset add --name draft_report --content-file draft-report.json --json
aig asset add --name review_policy --content-file review-policy.json --json
aig asset add --name review_instructions --content-file review-instructions.txt --json
aig task create --name guarded_claim_review \
  --description-file /path/to/aigineering/examples/guarded-claim-review/task-description.txt \
  --input research_result --input draft_report --input review_policy --input review_instructions \
  --activation 'research_result AND draft_report AND review_policy' \
  --label research_result --label draft_report --label review_policy --label review_instructions \
  --output review_report --budget 30 \
  --requires-capability planning --worker-pool planning \
  --delegate-capability claim.review.assess \
  --delegate-capability claim.review.revise \
  --delegate-capability claim.review.gate \
  --delegate-capability claim.review.semantic \
  --delegate-capability claim.review.accept \
  --delegate-pool validation --delegate-pool reasoning --delegate-pool verification \
  --acceptance-policy '{"mode":"independent","policy_version":"claim-review-closure-v1","required_attestations":1,"verifier_capabilities":["claim.review.accept"],"evidence_asset_ids":["SOURCE_ASSET_ID","DRAFT_ASSET_ID","POLICY_ASSET_ID"]}' \
  --json
aig fleet run --config /path/to/aigineering/examples/guarded-claim-review/workers.toml \
  --task ROOT_CONTRACT_ID --wait-timeout 300 --json
```

Sort the three actual evidence IDs lexicographically in the policy JSON.
Replace the three evidence placeholders with the exact committed Asset IDs
returned by the preceding `aig asset add` commands. The planner compiles the
stage dependencies from the description; this example contains no business
driver script and uses only the configured model Worker for model-backed
stages. Runtime retries remain ordinary protocol attempts.

If the deterministic gate rejects the draft or revised report, the runtime can
durably record `/fail` and leave the root unqualified or blocked. A retry is a
normal new protocol attempt within the one-revision bound; the workflow does
not automatically repair or consume a prior revision. A successful structural
gate still does not establish semantic truth: the semantic Worker must accept
the exact report before the acceptor can attest it.

The supplied draft is frozen from the first recorded claim-review attempt in
[report 063](../../reports/063-runtime-native-cases-2026-09-13.md); it is deliberately
incorrect and has no explicit quote bindings. `research-result-duplicate-id.json`
is a separate malformed-source fixture for a structurally unrepairable run.
It must be imported as `research_result` in a fresh domain. Inputs are synthetic.

The acceptor reads one unambiguous exact Contract ID from
its signed child description. Native planning inserts a compile stage, so
the immediate parent is not necessarily the original root. The runtime still
checks target policy, evidence, producer independence and actor authority.

See [live development evidence](../../reports/065-guarded-claim-review-2026-09-13.md), including failed attempts and the qualified report.
