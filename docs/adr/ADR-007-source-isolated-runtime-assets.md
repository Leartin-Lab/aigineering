# ADR-007: Source-Isolated Runtime Assets

**Status**: Accepted
**Date**: 2026-06-11

## Context

Agent runtimes mix outputs from many sources: users, LLM workers, tools, method
handlers, policies, skills, memory, and future remote workers. If all assets are
treated as equivalent text, a model can appear to mint runtime-owned facts,
forge observations, or confuse a tool result with a final answer.

Aigineering needs source isolation so that assets carry enough provenance for
authority, replay, and audit decisions.

## Decision

Assets carry source metadata, including origin, trust tier, minting identity,
source URI, and signature fields. Runtime-reserved asset names use protected
prefixes such as `_tool_obs_`, `_tool_call_`, `_plan_result_`, `_method_ctx_`,
`_skill_`, `_memory_`, and `_mcp_`.

Untrusted workers cannot mint protected runtime assets through normal output
projection. Reserved assets must be created by an authorized runtime path, such
as a system method contract or future signer policy.

Tool observations, method results, skills, policies, and memory are represented
as assets, but their source determines what they are allowed to prove.

## Consequences

- A worker can propose a protected-looking name, but projection rejects it
  unless the contract has the required authority.
- A tool observation is distinguishable from a final declared output.
- Skills and memory can be injected as context without granting them execution
  power or minting authority.
- Future cryptographic signing and trust policies can extend the existing
  provenance fields instead of changing the asset model.
