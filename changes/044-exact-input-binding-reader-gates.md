# 044: Exact input selection with compatible-reader gates

## Problem

Name-based inputs can admit later same-name Assets into derived work. Clearing
input roles to avoid that lookup would remove the original role-bound trust policy.
Older runtimes ignore unknown sensitive-policy keys and cannot enforce a new guard.

## Resulting design

`input_asset_bindings` in the signed sensitive policy freezes mapped roles to sorted,
unique exact Asset IDs. Disclosure and verification share one raw selection owner;
original trust and redaction checks still apply. Active missing, wrong-role or
tombstoned references fail closed, without fallback. Unmapped roles retain their
previous authorized semantics; unused inherited bindings remain inert.

Guard-bearing Contracts require `task:v6`, using the full v5 field owner and policy.
New admission refuses guard-bearing older identity domains. SQLite version 18 adds
an incompatible-reader floor without new tables or columns. Historical no-key IDs
and facts are preserved. See [ADR-032](../docs/adr/ADR-032-exact-input-binding-reader-gates.md).

`aig domain upgrade` backs up an existing old Store with private file permissions,
then invokes the existing transactional migration. Current schemas return a no-op;
missing and newer schemas fail before writable initialization or backup creation.
The operation changes schema metadata, not domain authority or task facts.

## Public compatibility

There is no new Contract wire field or Candidate envelope version. Databases opened
by the new writable runtime cannot be reopened with the older runtime, even without
v6 work. Stop old Workers before upgrade and retain the database backup. This is a
normal reopen gate, not support for active mixed-version processes or raw imports.
Old unbound inputs remain dynamic; this does not retroactively freeze every task.

## Required verification

Check existing identity bytes, v6 recomputation/tampering, exact role selection,
same-name injection, original trust rules, legitimate planning/tool continuations,
17-to-18 preservation/rollback, real old-wheel declaration and reopen rejection,
CLI backup/no-op/failures, language-neutral identity vectors and installed behavior.

## Closure

Development evidence belongs in [report 078](../reports/078-review-linked-task-revisions-2026-10-04.md).
Stable-release qualification, tagging and publication remain separate.
