# Change 005: Documentation information architecture

Status: Proposed
Target: next maintenance cycle

## Problem

The repository has clear document roles, but navigation and ownership are
implicit. As the protocol, Store, adapters, and release evidence grow, the same
concept can be summarized in several places and become inconsistent.

## Intended structure

- root documents remain the compact entry points: `README.md`, `DESIGN.md`,
  `ROADMAP.md`, `CONTRIBUTING.md`, and `SKILL.md`;
- `DESIGN.md` remains the sole implemented architecture truth;
- `docs/boundary-invariants.md` remains the normative runtime guarantee list;
- `docs/adr/` records decisions and consequences, not current status summaries;
- `changes/` records bounded transitions, migration, verification, and closure;
- `docs/reference/` may contain stable protocol, CLI, and deployment reference
  only when that material has an independent reader and tests;
- `reports/` contains reproducible acceptance evidence, never exploratory notes;
- `conformance/` contains language-neutral fixtures and their consumption rules.

Private research, review scratchpads, analogies, and speculative designs remain
outside release artifacts.

## Migration order

1. Add a public documentation index with one owner for every concept.
2. Add link, private-reference, and release-artifact architecture tests.
3. Move stable operational reference out of oversized entry documents without
   duplicating its normative statements.
4. Keep historical ADRs and changes immutable except for status and closure
   evidence; remove or archive obsolete public summaries.
5. Split `DESIGN.md` only when a section has a stable independent consumer;
   otherwise keep the current truth cohesive.

## Non-goals

- rewriting implemented architecture during a documentation move;
- publishing private engineering records;
- adding another roadmap, design truth, or invariant list;
- generating documentation that has no owner or executable verification.

## Exit criteria

- every public concept has one normative owner and an indexed route;
- root entry documents remain concise;
- public links and artifact contents are tested;
- no release artifact refers to private workspaces;
- moving documentation does not change protocol or runtime behavior.
