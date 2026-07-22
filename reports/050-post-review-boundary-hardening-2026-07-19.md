# 0.5 post-review boundary hardening evidence

Baseline: `dev@38f9d2f` plus package 7 working tree
Date: 2026-07-19
Environment: macOS, Python 3.11, SQLite reference Store
Scope: authenticated Worker coordination, completion/terminal atomicity,
planning recovery, provenance, diagnostic projection, and release artifacts

## Result

The ten confirmed review findings remain closed. The review also expanded the
unsigned renewal finding to the adjacent HTTP claim endpoint and closed both
with signed, registered-key-bound operational Candidates.

## Deterministic verification

- `pytest -q`: **1034 passed**, one upstream FastAPI/Starlette deprecation
  warning;
- `ruff check src/aigineering tests`: passed;
- `ruff format --check src/aigineering tests`: 202 files formatted;
- built `aigineering-0.5.0.tar.gz` and
  `aigineering-0.5.0-py3-none-any.whl` with build isolation disabled because
  dependencies were already present;
- `twine check`: wheel and sdist passed;
- isolated import sweep: all 118 shipped Python modules imported;
- artifact inspection: no legacy Engine, RuntimeIngress, MethodRegistry,
  context-overflow controller, or state serializer in the wheel;
- sdist includes the current public `SKILL.md`, `DESIGN.md`, conformance
  vectors, ADR-014, and ADR-015.

Focused regressions cover signed cross-replica claim/renew/submit, unsigned and
tampered rejection, command replay, atomic completion marker persistence,
terminal single assignment, invalid scaffold and mixed-plan atomic recovery,
method-result Contract provenance, and terminal-only blocker projection.

Package 7 additionally proves language-neutral Candidate JSON/signature
vectors, keyed HumanWorker behavior, persistent EngineWorker inner-domain
restart with late-result fencing, and policy-bound independent acceptance.
Attestations bind an exact policy ID/version and committed rubric/evidence
Assets; the Store transaction rejects a different Asset for an already
selected output slot.

## Real LLM scenario

Provider/model: DeepSeek API / `deepseek-v4-flash`
Credential handling: ignored `.env`; no credential in report, trace, or artifact

A clean SQLite fact domain ran a root compliance assessment that explicitly
required a plan and two dependent business subtasks:

```text
root -> plan task -> requirements_summary task -> final_report task
```

Observed:

- four execution cycles completed the root, plan task, and two children;
- the plan expanded exactly two dependent Contracts;
- `final_report` states the required 365-day retention and says implementation
  and tests are unverified because no evidence was supplied;
- root status is `completed`, outputs are satisfied, blockers contain only
  `terminal:complete`, and `silent_failure_risks` is empty;
- rejection and recovery counts are zero;
- root model usage recorded 591 prompt, 305 completion, and 896 total tokens.

An earlier sandboxed network attempt failed before provider access. The runtime
closed it as failed, released the claim, and scheduled recovery rather than
ending silently. It is failure-path evidence, not counted as the live pass.

This historical result was supplemented on 2026-07-22 by repeated fresh runs
described below.

## Limitations

This evidence supports the 0.5 single-node SQLite reference scope. It is not an
external security audit, internet deployment certification, distributed Store
claim, or proof of provider quality beyond the bounded scenario. TLS, rate
limiting, service authentication, and hostile-network hardening remain
deployment responsibilities.

## 2026-07-22 follow-up review

An additional `.omo` report was rechecked against the actual call graph rather
than accepted by severity label. Confirmed issues in path handling, tool-call
parsing, Worker-package validation, lease-renewal failure, trace-store
conformance, SQLite error classification, terminal-record scan cost, and
allowance exhaustion were fixed. Claims that `contract_from_dict` bypasses
canonical identity, that current lease renewal concurrently uses one SQLite
connection, and that local HTTP actor authentication requires an ambient API
key were rejected by existing admission tests, execution sequencing, and the
documented deployment boundary.

Responsibility refactoring reduced `EngineWorker._invoke` from about 190 to 99
lines, `_run_task_pool` from about 200 to 75, `claim_next_package` to 83, and
`SQLiteStore.commit_ingress_batch` to 52. `contracts_from_plan_asset` is 163
lines after separating wire parsing, scope checks, allowance containment and
Contract construction. The full deterministic suite passed with **1053
tests** in the initial pass; live-driven regressions brought the final verified
total to **1073 tests**. Ruff check and formatting also passed. The 0.5.0 wheel and sdist built
successfully, passed Twine metadata checks, imported all 111 shipped Python
modules in isolation, and retained DESIGN, SKILL, ADR-014/015 and the public
conformance vectors.

### Fresh multi-step LLM closure

Thirteen isolated DeepSeek runs were used as iterative system/Worker/E2E
evidence. They exposed and closed compatibility-schema output gaps, empty task
descriptions, invisible allowance constraints, invalid `&` activation that
could leave a graph silently disabled, shallow serialization of nested plan
parameters, ambiguous stage output instructions, missing fail/recovery task
instructions, unreadable usage accounting, and disclosure wording that made
models mistake Asset content for an unopened file handle.

The final unambiguous two-step transformation completed this topology:

```text
root
  -> plan.draft -> plan.dependencies -> plan.compile
  -> task1_extract_evidence[evidence_summary]
  -> task2_produce_report[final_report]
  -> root complete
```

It produced six terminal-complete Contracts, no rejection or recovery record,
and a `final_report` grounded only in the two disclosed sample strings. Six
claim-bound usage traces recorded 5,074 prompt, 4,477 completion, and 9,551
total tokens. The root returned `status=complete`, `ok=true`, and only
`terminal:complete` as a blocker.

Separate runs exercised conservative refusal. A business Worker published
`/fail`; the ordinary `plugin:fail` task produced its declared failure result,
the parent failed visibly, and the root reported `descendant_failed`. When a
recovery Candidate exceeded causal allowance, the runtime recorded
`recovery_unavailable`, not a false scheduled recovery. These are accepted
failure-path results, not successful productivity runs.

### Release-artifact reverse review

A final reverse review found that preliminary sibling promises could authorize
a consumer before the producer later failed description or allowance checks.
The compiler now computes a final reachability closure from accepted producers
only; ungrounded cycles, self-dependent tasks, and consumers of rejected
producers are rejected instead of becoming permanently disabled. The generated
compile example also scales to one remaining allowance unit and covers every
required output.

The resulting deterministic gate is **1076 passed** in 46.25 seconds, with Ruff
check and format clean. A freshly rebuilt 0.5.0 wheel was installed into a
separate virtual environment and loaded from `site-packages`. Independent CLI
processes initialized a signed domain, committed an input and task, executed a
mock Worker, then reopened the same SQLite database: the task reconstructed as
completed, the output was an observed Worker-signed Asset, and silent-failure,
rejection, and recovery projections were empty.

The allowance-aware prompt fix was then exercised in a fourteenth isolated
DeepSeek run with root allowance exactly 3. Draft and dependency analysis each
consumed one unit, leaving compile exactly one unit. The model published one
ordinary business child, all five Contracts reached `complete`, and the final
LLM-signed observed Asset was exactly the disclosed text `Alpha evidence.`.
There were zero rejections, recoveries, or silent-failure risks; claim-bound
usage across the five tasks totaled 8,149 tokens. This specifically proves the
minimum-allowance compile example rather than extrapolating from the earlier
two-child run.

### Long-chain and recursive-replan acceptance

Two further isolated DeepSeek runs exercised graph depth and dynamic planning
rather than another short happy path.

The long-chain task required exactly 20 sequential business transformations.
The runtime published 24 Contracts (root, three planning stages, and 20
business tasks). All 24 completed. Every intermediate checkpoint matched the
expected cumulative string, and `final_report` was exactly
`S0|01|02|03|04|05|06|07|08|09|10|11|12|13|14|15|16|17|18|19|20`.
The database projected zero rejections, recoveries, or silent-failure risks;
claim-bound usage totaled 28,913 tokens. During execution, the next task was
blocked only by its predecessor fact and became ready naturally when that fact
committed.

The recursive-replan task began from an invalid v1 premise. The root published
`/replan`; its replacement `check_v2_assumption` task then discovered that v2
was also invalid and independently published a second `/replan`. Both replan
cycles executed draft, dependency-analysis, and compile Contracts. The final
graph contained 10 Contracts, all completed, with `verified_basis` and
`final_report` both exactly `blue`. It recorded zero rejections, recoveries, or
silent-failure risks and 17,586 total tokens. This demonstrates recursive
planning as ordinary task publication rather than hidden Engine control flow.
