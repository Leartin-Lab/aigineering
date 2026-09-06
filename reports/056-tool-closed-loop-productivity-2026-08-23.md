# v0.5.6 tool closed-loop productivity evidence

Date: 2026-08-23

Baseline commit: `6f7e3fb2638ce6d0aa41ea1ada95ddce9f70180d`

Candidate state: v0.5.6 implementation in the reviewed worktree. No credential
or generated live Store is tracked by the repository.

Environment: Darwin 25.5.0 arm64, Python 3.11.15, SQLite 3.51.0, Twine 7.0.0.

## Scope

This report covers executable local tool contracts, signed execution metadata,
the descendant productivity projection, runtime-only AI4S acceptance,
materialization reconstruction, distribution artifacts, and one bounded live
Fleet run. It does not claim production MCP transport, process-level tool
isolation or cancellation, exactly-once external side effects, cross-machine
discovery, or scientific truth.

## Deterministic evidence

- `ToolSpec` input/output schemas, version, and UTF-8 byte limit are validated
  at registration and execution. Invalid input, malformed JSON output,
  impossible/unsupported schema documents, oversized output, and descriptor
  drift fail closed as typed observations.
- Tool execution metadata survives WorkerHost signing, Candidate commitment,
  SQLite reopen, deletion and reconstruction of every materialization, and the
  descendant `task audit --json` productivity projection.
- The runtime-only AI4S test starts from one ordinary root `/plan`. Staged
  planning creates the producer and independent verifier; ToolWorker commits
  the OpenAlex observation; a continuation publishes the report; the verifier
  submits `/attest`; and qualification completes the root without importing or
  calling `examples/ai4s/audit.py`.
- The candidate-rejection recovery Fleet test now compares the runtime
  materialization digest before and after reconstruction. Twenty consecutive
  repetitions passed, including v4 child identity, frozen Skill context,
  explicit rejection, recovery publication, output completion, and rebuild.
- Architecture guards prohibit the productivity projection, CLI audit,
  ToolWorker, or ToolExecutor from becoming alternate Store ingress paths and
  prohibit the new core projection from importing Plugin semantics.

Final deterministic commands:

```text
ruff check src/aigineering tests examples/ai4s/tools.py
ruff format --check src/aigineering tests examples/ai4s/tools.py
pytest -q
```

The closing suite passed 1,221 tests with 3 intentional skips and one upstream
Starlette/httpx deprecation warning. Ruff check passed and 235 files were
already formatted.

## Bounded live Fleet evidence

Provider: SiliconFlow OpenAI-compatible API.

Model: `deepseek-ai/DeepSeek-V4-Flash`.

Retrieval: bundled fixed OpenAlex response and fixed retrieval timestamp; no
live OpenAlex request was made. The test credential was injected through a
non-echoing process input and was not written to the repository, command line,
report, or generated configuration.

Passing root Contract:
`task:v5:016e7fb71a7b5a9f049114501cf43c2516e5f4f8a19124960029a6bf3c128eb6`.

Observed result:

- Fleet status `complete`; root task status `completed`; declared output
  satisfaction true; no timeout and no silent failure risk;
- all 12 Contracts reached terminal facts: 11 complete and one failed attempt;
- one planning request was explicitly rejected, its original attempt remained
  failed, and one ordinary recovery Contract completed the same bounded work;
- descendant audit retained two rejected trace fragments and the completed
  recovery instead of hiding them;
- one `openalex_search` call succeeded, produced 1,075 UTF-8 bytes under tool
  contract version `1.0.0`, and enabled one continuation;
- recorded model usage was 30,346 prompt tokens, 3,709 completion tokens, and
  34,055 total tokens;
- the report contained exactly `answer`, `citations`, and `limitations`; both
  cited IDs were members of the three-record committed retrieval manifest;
- the report producer was the synthesis Contract while `literature-verifier`
  was the distinct attesting actor; one `asset.attested` and one
  `output.qualified` record bound the exact report Asset;
- a SQLite backup taken before reconstruction and the rebuilt Store had the
  same semantic materialization digest
  `58d24f8db2087f77250c9b3e582162e455a0e1ff2876a857474d0bf301a626bb`;
  task status and the complete productivity payload were byte-for-byte equal
  as decoded JSON structures before and after rebuild.

An earlier diagnostic live run also completed all 12 Contracts with one tool
continuation and recovery. Its first rebuild digest assertion did not match,
but no pre-rebuild backup had been retained, so that run is not used as passing
reconstruction evidence. A second rebuild was stable. The backup-first passing
run above and the 20 repeated deterministic rejection/recovery rebuilds did not
reproduce the mismatch. This negative round remains recorded rather than being
silently discarded.

## Artifact and installation evidence

An isolated PEP 517 build produced:

- `aigineering-0.5.6.tar.gz`;
- `aigineering-0.5.6-py3-none-any.whl`.

Twine 7 accepted both Metadata 2.5 artifacts. The source distribution contains
ADR-020, change 014, this report, the public Skill, and the AI4S example. A new
Python 3.11 virtual environment installed the wheel and its declared
dependencies, imported version `0.5.6`, initialized a signed domain, and
exposed `aig task create`, `status`, `wait`, and `audit`.

## Remaining limits

The live run proves bounded local runtime productivity and observable recovery,
not the semantic truth of the literature answer or a hostile-network profile.
The Python tool handler is still synchronous and in-process. `retryable`
metadata is evidence, not automatic safe retry for mutating effects. Production
MCP transport, forced timeout/cancellation/isolation, external-effect
idempotency or compensation, and cross-machine discovery remain future work.

## Commit preparation verification — 2026-09-06

The v0.5.6 worktree based on `6f7e3fb2638ce6d0aa41ea1ada95ddce9f70180d`
was rechecked before its implementation commit on `dev`. This is local commit
acceptance, not evidence of a published GitHub release or PyPI upload.

- Python 3.11.15: `pytest -q` passed 1,221 tests with 3 Redis integration
  skips and the existing Starlette/httpx deprecation warning (53.26 seconds).
- Ruff check and format passed over runtime, tests, and `examples/ai4s/tools.py`
  (235 files formatted); `git diff --check` passed.
- `python -m build` produced wheel and sdist in an isolated build environment;
  Twine 7 accepted both distributions.
- A fresh Python 3.11 environment installed the wheel and declared dependencies.
  Separate CLI processes initialized a signed domain, created a task, ran an
  explicit mock Worker, and reopened completed status and productivity audit.
- The existing live Fleet and reconstruction evidence above was not rerun.
  The unexplained diagnostic digest mismatch remains open; non-reproduction
  is not treated as a root-cause resolution.
