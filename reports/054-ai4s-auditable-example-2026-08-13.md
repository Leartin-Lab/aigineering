# v0.5.4 AI4S auditable example evidence

Date: 2026-08-13

## Scope

This report records release evidence for configured tool execution, exact
derived-evidence verification, and the executable AI4S literature example. It
does not claim that model prose or external scholarly metadata is scientifically
true.

## Deterministic evidence

- the offline LLM transport test requests an OpenAlex tool through the provider
  protocol, routes the ordinary tool task to a separate ToolWorker, commits its
  observation, resumes an LLM continuation, and produces a JSON report;
- before attestation the report Asset exists but the root task is not complete;
- a distinct verifier rejects fabricated citation IDs and accepts only IDs in a
  successful committed descendant observation;
- accepted attestation qualifies the exact descendant Asset, and SQLite reopen
  reconstructs the root task as completed;
- exact line, character, and UTF-8 byte slice tests recompute source content and
  reject forged content, out-of-range slices, and UTF-8 splits;
- Windows-specific SQLite handle and TOML path regressions are included in the
  deterministic suite. Enabling the remote Windows CI matrix remains pending
  a GitHub credential with `workflow` scope; it is not claimed as executed
  release evidence.

The bounded regression selection passed 150 tests. After the final routing
constraint, the release-wide suite passed **1174 tests with 3 intentional
skips**. Ruff check and format passed over runtime, tests, and examples. The
isolated build produced the 0.5.4 wheel and sdist; current Twine accepted both,
the sdist contains all public AI4S assets, and a clean installed wheel completed
domain initialization plus task create/run/status smoke checks.

## Live evidence

Three live runs used `deepseek-v4-flash` through the configured OpenAI-compatible
provider and the live OpenAlex Works API:

1. The initial run exposed a same-tool continuation loop. Four successful tool
   observations exhausted causal allowance and ended in an explicit failed
   descendant; no root success was reported. The runtime now removes the used
   tool from a successful continuation scope.
2. The repaired scientific-question-answering run made one tool call, retrieved
   five of 25,213 matching works, and produced a report using all five stable
   IDs. Before attestation `aig run` exited non-zero with
   `outputs_satisfied=false`. Independent attestation accepted 5/5 IDs, task
   status became `completed`, and a new SQLite process reconstructed terminal
   `complete`. Provider usage was 1,280 tokens.
3. A different metadata-limit question repeated the result: one tool call,
   non-success before attestation, 5/5 citation membership, then `completed`
   with no rejection or silent-failure risk. Provider usage was 1,275 tokens.

A separate direct OpenAlex adapter run applied a 2023–2026 filter, returned
three stable records out of 23,637 matches, and recorded truncation explicitly.

For the third run, deletion and reconstruction of all SQLite materializations
preserved the exact semantic digest
`ebe044077e37289632b03c372688222726306d57146622dc32928b851de991b3` and
the task remained completed afterward.

The live verifier checked citation membership and task ancestry. The runs do
not establish full-text support or scientific correctness of the generated
prose.

## Limits

The verifier proves citation membership, task ancestry, Candidate identity,
and acceptance-policy binding. It does not read full papers, validate causal
claims, rank evidence quality, or replace scientific peer review. Tool registry
factories are trusted local operator code and are never loaded by default.
