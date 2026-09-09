# 018: Runtime primitive convergence

Status: implemented
Version: v0.5.10
Date: 2026-09-08

This compatibility release reduces duplicated runtime semantics without changing
the Candidate-to-Fact boundary, canonical wire values, SQLite schema, or public
CLI behavior.

The implementation converges two repeated mechanisms:

- canonical construction of immutable Contracts and their versioned identity;
- stateless runtime maintenance shared by CLI, local Fleet, and nested Workers.

The release also removes production dependencies on source-compatibility modules
and fixes recovery paths that fail to retain the source Contract's complete
acceptance and containment policy.

## Non-negotiable compatibility

- Existing v3, v4, and v5 Contract identities remain valid.
- Candidate and RuntimeRecord wire representations do not change.
- SQLite remains the authoritative Store and no transaction is split.
- Retry, recovery, continuation, and planning publish ordinary signed Candidate
  effects.
- Rejection, claim fencing, terminal single assignment, and reconstruction remain
  observable and deterministic.

## Exit criteria

- All new Contract publication uses one canonical construction helper.
- Recovery-equivalence tests cover acceptance, disclosure, routing, delegation,
  tool, context, and minting fields.
- Repeated runtime maintenance order is represented by one stateless primitive.
- Production modules no longer import the `core.methods` compatibility facade.
- Ruff, the full deterministic test suite, package build, and SQLite reopen gates
  pass.

Splitting the large SQLite implementation into internal materializers and ledgers
remains follow-up work. It was deliberately excluded here because changing file
ownership and transaction structure in the same slice would weaken the evidence
that this release is behavior preserving.
