# Runtime-native case evidence — 2026-09-13

Two declarative cases were exercised against v0.5.11 at runtime commit
`0b15f61c1a6fd59ed4548c72c6ba8e4e5a9d17e8`, using the real `deepseek-flash`
model through `https://api.deepseek.com/v1`. The numerical report passed manual
review. The claim-evidence case completed the runtime protocol in four attempts,
but each attempt failed at least one manual criterion. It is a diagnostic case,
not evidence of reliable autonomous semantic verification.

## Execution boundary

Each attempt started in an empty domain. Setup used ordinary CLI commands to
load input Assets, optionally load a Skill, and create **one root Contract**.
One `aig fleet run` invocation then ran that root. The first Worker action was
`/plan`; the existing draft/dependencies/compile mechanism published the business
children. There was no business orchestration script, manually published child
Contract, prebuilt DAG, edited output, or external retry driver. Root descriptions
do prescribe the intended stages and handoff names; the model is not discovering
the workflow from an unconstrained goal.

Every attempt produced eight completed Contracts: the root, three planning
Contracts, and four business Contracts. Each final output received an independent
Worker attestation and root output qualification. Runtime rejection and recovery
counts were zero in all five runs. These facts establish protocol completion,
not semantic correctness.

The examples contain JSON inputs and acceptance policies, task descriptions,
Worker TOML, and documentation. The numerical case also loads a declarative Skill.
`ORACLE.md` files are manual review criteria and were not loaded into any task or
Skill. No expected total or verdict answer list was disclosed. Audit export and
backup/rebuild diagnostics were read-only post-run evidence collection, not part
of business execution. Credentials and private databases are not published.

## Results

| Run | Calls | Prompt / completion tokens | Total tokens | Manual review |
| --- | ---: | ---: | ---: | --- |
| [Numerical report](data/063-runtime-native-cases/report.json) | 8 | 19,105 / 3,919 | 23,024 | Passed |
| [Claims: initial](data/063-runtime-native-cases/claims-initial.json) | 8 | 17,252 / 4,308 | 21,560 | Failed original-claim preservation and source grounding |
| [Claims: preserved source](data/063-runtime-native-cases/claims-intermediate.json) | 8 | 19,902 / 5,217 | 25,119 | Failed citation completeness |
| [Claims: explicit citation rule](data/063-runtime-native-cases/claims-citation-tightened.json) | 8 | 19,213 / 4,542 | 23,755 | Failed predefined verdict convention |
| [Claims: explicit verdict definitions](data/063-runtime-native-cases/claims-taxonomy.json) | 8 | 21,159 / 5,097 | 26,256 | Failed citation completeness |

Total recorded usage was 119,714 tokens across 40 model calls. This is reported
usage, not a price estimate. These are iterative fixture-development runs, not an
independent benchmark or a statistical success-rate estimate.

### Numerical report consistency

[Run instructions](../examples/report-consistency/README.md) load a source ledger,
a deliberately inconsistent draft, and a generic checking standard. Workers
extract the ledger, compare the draft, produce a corrected audit report, and
verify that exact report. The output contains months `[120, 150, 200]`, total
`470`, and findings identifying the draft's incorrect March value and total.
It preserves the source Asset ID, report ID, and currency. Manual comparison
against the separate oracle passed.

The configured economy Worker was unused in this run: the reasoning Worker
handled seven calls and the verifier handled one. Thus this run demonstrates
two participating Worker identities, not a measured efficiency gain from the
three configured profiles.

### Claim-evidence review

[Run instructions](../examples/claim-evidence-review/README.md) load synthetic
study excerpts and three claims. The workflow extracts evidence, checks claims,
revises the report, and verifies it. The source is a cross-sectional study with
two-campus sampling; the claims include descriptive, causal, and population-wide
statements. No external literature retrieval occurs.

1. The [initial task](data/063-runtime-native-cases/initial-claim-task.txt)
   produced a qualified report that rewrote all original claim texts, replaced
   the third claim with a different proposition, and incorrectly said sampling
   details were absent. Independent LLM attestation missed these problems.
2. The [source-preserving task](data/063-runtime-native-cases/intermediate-claim-task.txt)
   required every business child to receive the original source alongside its
   handoff and preserve exact claim text. It also routed extraction/checking to
   the economy pool, revision to reasoning, and verification to its own pool.
   Original claims and substantive judgments were correct, but the third
   rationale used the limitations excerpt without listing its ID.
3. The [citation-tightened task](data/063-runtime-native-cases/citation-tightened-claim-task.txt)
   explicitly required all relied-on excerpts to be cited. Citation review
   passed, but the causal claim was labeled `unsupported` rather than the
   predeclared oracle's `overstated`. Its explanation and calibrated revision
   were sound. The task had not yet defined the distinction between these labels;
   this exposed a specification ambiguity as well as a failed strict criterion.
4. The [current task](../examples/claim-evidence-review/task-description.txt)
   defines the labels generically without supplying per-claim answers. All
   verdicts matched the oracle, but the first rationale used residual confounding
   and recall error without listing `excerpt-limitations`. Its revised wording
   also moved from higher group mean to a probability-like “more likely” claim,
   which is not established by a mean comparison alone. This attempt remains a
   failed semantic review despite runtime qualification.

The latter three attempts used all three Worker identities. All business
handoffs and direct original-source inputs were present in the last two runs.
The verifier's separate identity, capability, and pool enforce runtime authority
separation. Every Worker still uses the same underlying model; this is not
independence of training, provider, or likely reasoning errors. The acceptance
policy enforces output shape and attestation requirements; the semantic checks
in these examples are instructions to an LLM, not deterministic validators.

## Durability evidence and limits

All five stores passed backup-first reopen/rebuild diagnostics. Each exported
JSON includes matching before/after materialization digests and
`records_unchanged: true`. Physical table hashes changed for `trace_events`,
`worker_claims`, and `worker_registrations` during rebuild normalization; the
claim is semantic reconstruction, not byte-identical database files.

The numerical run is a small positive fixture, not a guarantee for arbitrary
reports. The claim runs show observable, recoverable evidence of semantic
failure, but no rejection/recovery was exercised: the LLM verifiers accepted
them. These cases do not demonstrate automatic method creation, method promotion,
heterogeneous Worker implementation types, or live external tool integration.
A dedicated source-preservation/citation/semantic validation method remains
unimplemented in this case. The preserved failures provide concrete inputs for
that next step; a stronger prompt alone has not established reliable closure.

## Repository validation

`ruff check` and `ruff format --check` passed. The final full local test run
reported **1,349 passed, 3 skipped**. Both isolated and non-isolated builds
succeeded, and `twine check` passed for the wheel and source distribution.
The source distribution includes both declarative cases. JSON/TOML parsing,
public Markdown link checks, and scans of the new evidence for private paths
and credentials passed. No runtime implementation was changed by this work.
