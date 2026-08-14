# v0.5.5 local Worker fleet evidence

Date: 2026-08-14

## Scope

This report covers Contract-v5 delegated routing scope, same-machine
heterogeneous Worker concurrency, parallel tool compilation, durable malformed
output recovery, and the runtime-compiled AI4S example. It does not claim
cross-machine discovery, public-network hardening, or scientific truth.

## Deterministic evidence

- two independently routed Workers meet at a synchronization barrier, submit
  different parent outputs through separate SQLite connections, and reconstruct
  one completed root task;
- a lower-level concurrent commitment test forces both pure reducers to observe
  stale snapshots, then proves commit-time reduction emits exactly one terminal
  fact and one completion trace;
- malformed claim-bound output closes the original attempt, persists signed raw
  evidence, creates one recovery task, preserves the exact Skill Asset IDs and
  labels, and completes the parent through a repaired output;
- two tool handlers meet at a synchronization barrier, commit independent
  observations, satisfy an `AND` activation, and enable one continuation;
- Fleet configuration is strict, contains no secret value, and routes by
  capabilities and pools rather than model price.
- staged planning retains bounded causal headroom for one repair of each stage,
  preventing an early formatting failure from consuming the compile path.

The closing deterministic run passed 1,206 tests with 3 intentional skips.
Ruff check and format check passed across `src/aigineering` and `tests`.

## Final gate

- `pytest -q`: 1,206 passed, 3 skipped;
- `ruff check src/aigineering tests`: passed;
- `ruff format --check src/aigineering tests`: 230 files already formatted;
- isolated PEP 517 build produced `aigineering-0.5.5.tar.gz` and
  `aigineering-0.5.5-py3-none-any.whl`;
- Twine 7 accepted both Metadata 2.5 artifacts;
- a clean Python 3.11 virtual environment installed the wheel, initialized a
  signed domain, and exposed the `aig fleet run` command;
- the sdist contains ADR-019, change 013, this report, the public Skill, the
  AI4S Fleet/query files, and the installable literature Skill metadata.

## Live evidence

The bounded passing run used `deepseek-v4-flash` through the declared local
Fleet and a fixed OpenAlex response. The public root Contract was
`task:v5:45f3e4e08c1557f83245dd5cf712e046ad37f6d9698f2d44d81a40646c6fb723`.
Only the Skill, query Asset, root Contract, and Fleet configuration were
published by the operator; no example DAG driver ran.

The runtime compiled draft, dependency, compile, retrieve, screen, extract,
synthesize, verify, tool, continuation, and recovery tasks. Two planning-stage
provider failures became durable failed attempts and two independently
claimable recovery tasks. All 13 Contracts reached an explicit terminal fact.
The root projection was `completed`, declared output satisfaction was true,
and `silent_failure_risks` was empty.

The committed retrieval manifest contains exactly the three stable IDs from
the configured fixture. The final report is one JSON object with exact keys
`answer`, `citations`, and `limitations`; its citations are the observed
`W0000000001` and `W0000000002` IDs, and both arrays are non-empty string
arrays. The report was signed by `literature-reasoning`. A distinct
`literature-verifier` key signed the exact verification receipt and
`asset.attest`; commitment created `output.qualified` for that exact report
Asset before the root terminal fact.

Earlier negative rounds were retained as diagnostic evidence. They exposed and
then closed: cancellation of a verifier after an intermediate parent completed,
mixed atomic groups in `/attest`, a verifier confusing its own Contract ID with
the acceptance target, fabricated retrieval data during recovery, provider
quoting of a tool action inside `/exec`, and LLM review accepting the wrong JSON
field type. The final design does not rely on prompts for those boundaries:
tool recovery requires committed observations, one unambiguous quoted method is
normalized through the canonical parser, and deterministic `output_shapes` are
enforced at producer submission and qualification while remaining inherited by
planning and recovery work.

This evidence demonstrates bounded local productivity and failure recovery. It
does not claim that the literature answer is scientific truth, that one model
verifier is a general quality oracle, or that the local reference runtime is a
hostile-network deployment.
