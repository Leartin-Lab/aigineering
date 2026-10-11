# Guarded claim review

This is a declarative root-task example for a single bounded revision followed
by deterministic gating and a separate semantic review. The operator submits
three base Assets plus the frozen `review_instructions` document first: `research_result`, `draft_report`, and
`review_policy`. The root task freezes all four in context and passes them to
every child. Planner-created handoffs are `draft_checks`, `review_report`,
`structural_receipt`, `semantic_review`, and `acceptance_receipt`.

The local assessor and gate perform deterministic checks. The reviser and
semantic reviewer are configured for one model call per attempt through the
local factory, using
`DEEPSEEK_API_KEY`, `AIG_REVIEW_MODEL` (default `deepseek-flash`), and
`AIG_REVIEW_BASE_URL` (default `https://api.deepseek.com/v1`). The acceptor has
the only `asset.attest` capability. Worker roles and policy actors are distinct.

The preset publishes the four explicit input Assets and the signed root, then
starts the configured Workers through the shared application service:

```bash
export DEEPSEEK_API_KEY=replace-me
export AIG_REVIEW_MODEL=deepseek-flash
export AIG_REVIEW_BASE_URL=https://api.deepseek.com/v1
mkdir -p workspaces/guarded-claim-review
aig -C workspaces/guarded-claim-review run --config /path/to/aigineering/examples/guarded-claim-review/run.toml \
  --wait-timeout 300 --json
# Use the returned contract_id for continuation or audit:
aig -C workspaces/guarded-claim-review run --config /path/to/aigineering/examples/guarded-claim-review/run.toml \
  --resume TASK_ID --wait-timeout 300 --json
aig -C workspaces/guarded-claim-review task audit TASK_ID --json
```

The preset binds the three declared evidence input names to the actual committed
Asset IDs in the acceptance policy before publishing the root. The planner compiles the
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

The preset paths are relative to `run.toml`; its database is relative to the
selected workspace. Use a fresh workspace for an independent run; a new start
refuses historical outputs. Resume does not reload inputs or reset a failed root.
See [configured runs](../../docs/reference/configured-runs.md). The entrypoint
migration has deterministic verification; the earlier linked live reports do not
constitute new provider execution of this preset.
