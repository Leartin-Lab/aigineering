# v0.5.0 release acceptance

Runtime implementation commit: `3c9d74b`
Scope: single-machine SQLite reference runtime
Status: passed

## Boundary coverage

The release suite verifies:

- actor-signed Candidate admission and fail-closed key/domain/capability checks;
- pure effect projection and declared-output authority;
- reserved namespace containment;
- claim/package/epoch fencing and authenticated renewal;
- atomic Candidate, fact, trace, idempotency, terminal, and claim commitment;
- causal allowance reservation and concurrent overspend rejection;
- independent exact-Asset output attestation;
- visible rejection, provider failure, expired claim, and unavailable recovery;
- terminal uniqueness and descendant failure projection;
- materialization deletion, rebuild, and semantic-digest equality;
- same-machine active-active Worker arbitration;
- EngineWorker inner-domain restart and late-result fencing;
- language-neutral signed JSON and public protocol vectors.

## Deterministic verification

Final deterministic gate:

```text
1077 passed
ruff check: passed
ruff format --check: passed
```

## Artifact verification

The v0.5.0 wheel and sdist:

- build successfully through the declared PEP 517 backend;
- pass Twine metadata checks;
- contain the public design, ADRs, Skill, change summaries, release evidence,
  and conformance vectors;
- exclude tests and removed runtime modules;
- import every shipped Python module.

A freshly installed wheel was loaded from an isolated `site-packages`
directory. Independent CLI processes initialized a domain, committed an input
and task, executed a Worker, reopened the same SQLite database, and reconstructed
the completed task and signed output with no rejection, recovery, or silent
failure risk.

## Real-LLM acceptance

Provider: DeepSeek OpenAI-compatible API
Model: `deepseek-v4-flash`
Credential handling: ignored local environment file; no credential persisted in
the repository, database evidence, trace output, or report.

Sixteen isolated runs tested system behavior, individual Worker task schemas,
and end-to-end composition. Failures found during the sequence became
deterministic regressions before the next run.

Validated task types included:

- ordinary execution;
- plan draft;
- plan dependency analysis;
- plan compile;
- replan draft, dependency analysis, and compile;
- business child execution;
- explicit fail;
- recovery unavailable;
- minimum-allowance compile;
- long serial fact propagation;
- nested replanning.

### Minimum planning allowance

A root allowance of 3 produced one draft task, one dependency task, and a
compile task with one remaining unit. Compile published one business child.
All five Contracts completed with exact expected output, zero rejection,
recovery, and silent-failure risks.

### Twenty-step serial chain

The model published exactly 20 sequential business tasks:

```text
root
→ plan.draft
→ plan.dependencies
→ plan.compile
→ step_01
→ ...
→ step_20
→ root complete
```

All 24 Contracts completed. Nineteen intermediate checkpoints matched their
exact cumulative oracle. The final output was:

```text
S0|01|02|03|04|05|06|07|08|09|10|11|12|13|14|15|16|17|18|19|20
```

The graph recorded zero rejection, recovery, and silent-failure risks.
Claim-bound usage totaled 28,913 tokens.

During execution, the next task was blocked only by the absence of its
predecessor fact and became ready naturally when that fact committed. No
process-owned waiting state was required.

### Nested replanning

The root task discovered that its v1 premise was invalid and published
`/replan`. Its replacement validation task then discovered that v2 was also
invalid and independently published a second `/replan`.

Both cycles executed ordinary draft, dependency-analysis, and compile tasks.
All 10 Contracts completed. `verified_basis` and `final_report` were both
exactly `blue`, with zero rejection, recovery, and silent-failure risks.
Claim-bound usage totaled 17,586 tokens.

### Visible failure

Separate scenarios exercised explicit `/fail`, provider failure, invalid
activation, rejected planning output, and recovery publication without
allowance. Each produced a durable failure, blocker, rejection, or
`recovery_unavailable` record. None returned success or left an active claim.

## Limits of the evidence

This report supports the documented v0.5.0 local reference scope. It is not:

- an external security audit;
- a public-network deployment certification;
- a cross-machine consistency proof;
- a guarantee of model truth outside the bounded scenarios.
