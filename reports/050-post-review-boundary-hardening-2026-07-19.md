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

This live result is retained historical evidence from the same 0.5 line. A
fresh package-7 repetition was attempted but external network escalation was
unavailable in the current environment; it is therefore not reported as a new
pass and remains a release gate.

## Limitations

This evidence supports the 0.5 single-node SQLite reference scope. It is not an
external security audit, internet deployment certification, distributed Store
claim, or proof of provider quality beyond the bounded scenario. TLS, rate
limiting, service authentication, and hostile-network hardening remain
deployment responsibilities.
