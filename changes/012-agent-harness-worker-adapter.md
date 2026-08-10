# Change 012: Agent harness Worker adapter

Status: Implemented and locally verified on dev
Target: v0.5.3 stabilization
Decisions: ADR-006 and ADR-012

## Problem

The runtime supports authenticated pull Workers, but an existing agent harness
must currently reconstruct package parsing, action compilation, graph output,
claim commands, signatures, and idempotency itself. Public examples also place
mock execution before real provider configuration. This makes the safest path
harder than the test path.

## Intended change

- expose one tested `HarnessCandidateAdapter` for claim, renewal, and result
  Candidates;
- delegate result compilation to the same function used by WorkerHost;
- normalize complete reasoning wrappers and JSON-valued LLM output content at
  the provider adapter before strict action parsing;
- preserve existing harness orchestration inside the Worker invocation;
- make LLM the CLI execution default while keeping mock explicit;
- document a framework-neutral pull/submit migration and completion contract.

## Non-goals

- embedding a specific agent framework in the kernel;
- treating harness logs or conversation state as facts;
- storing Worker private keys in the runtime;
- making the reference HTTP server a hostile-network deployment profile;
- removing MockWorker from deterministic tests.

## Exit criteria

- built-in and harness Workers share one action-to-effect compiler;
- provider presentation normalization does not relax Candidate validation;
- claim, renewal, `/exec`, and `/plan` adapter paths have behavioral tests;
- normal CLI help and public examples prefer configured LLM execution;
- mock remains available only through explicit selection;
- a fresh harness loop produces a committed, reconstructable result with no
  direct Store mutation.
