# ADR-008: Unified runtime ingress

Status: Superseded by ADR-011
Date: 2026-06-23
Related: ADR-001, ADR-003, ADR-005, ADR-007, ADR-011

## Context

Runtime facts once entered through multiple caller-specific paths with
different signing, namespace, authority, trace, and transaction behavior.

## Decision

ADR-008 introduced one ingress owner so every fact followed the same
protection, commitment, trace, and reduction rules.

## Supersession

ADR-011 completed the boundary by replacing the ingress facade with
actor-signed typed Candidates and one commitment reducer. The
`RuntimeIngress` implementation is not part of v0.5.0.

The lasting decision is:

> A user interface or transport may vary, but it may not own alternate fact
> mutation semantics.

Current implementation truth is documented in `DESIGN.md`.
